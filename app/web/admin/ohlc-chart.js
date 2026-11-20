'use strict';
/** Shared OHLC + entry-zone canvas for Admin mini + popup.
 *  Y-scale is candle-first: KAMA warm-up zeros / broker TP 0 must never
 *  stretch the axis (that flattened GOLD into a hairline at ~3700).
 */

function _ohlcFinite(v){
  // Number(null)===0 — must not paint ADX 0 / ATR 0.00 when MTF is not ready.
  if(v==null||v==='')return null;
  const n=Number(v);
  return Number.isFinite(n)?n:null;
}

/** Keep Y-scale on the tape — drop unset 0 / warm-up zeros / wild outliers. */
function _ohlcNearTape(v,anchor,span){
  const n=_ohlcFinite(v);
  if(n==null||!(n>0))return null;
  if(anchor==null||!Number.isFinite(anchor))return n;
  const band=Math.max(
    (Number.isFinite(span)&&span>0?span:Math.abs(anchor)*0.002)*6,
    Math.abs(anchor)*0.012,
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
  const bw=Math.round(w*dpr),bh=Math.round(h*dpr);
  // Avoid resetting the bitmap every poll — that flashes the whole chart.
  if(canvas.width!==bw||canvas.height!==bh){
    canvas.width=bw;canvas.height=bh;
  }
  const ctx=canvas.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,w,h);
  ctx.fillStyle='#060a14';
  ctx.fillRect(0,0,w,h);

  let candles=(view.candles||[]).filter(c=>{
    if(!['open','high','low','close'].every(k=>Number.isFinite(Number(c[k]))))return false;
    // Drop garbage / zero bars that would collapse the Y scale.
    return Number(c.low)>0&&Number(c.high)>0&&Number(c.high)>=Number(c.low);
  });
  const maxN=Math.max(20,Number(opts.maxCandles)||candles.length);
  const slicedOff=candles.length>maxN?candles.length-maxN:0;
  if(slicedOff)candles=candles.slice(-maxN);
  if(!candles.length){
    ctx.fillStyle='#8a9a92';ctx.font='13px Manrope,sans-serif';ctx.textAlign='center';
    ctx.fillText('Gaida OHLC sveces…',w/2,h/2);
    return;
  }

  // Base range = candles only so bars fill the viewport.
  let candleLow=Math.min(...candles.map(c=>+c.low));
  let candleHigh=Math.max(...candles.map(c=>+c.high));
  const mid=_ohlcFinite(view.mid);
  const anchor=mid!=null&&mid>0?mid:(candleLow+candleHigh)/2;
  const candleSpan=Math.max(candleHigh-candleLow,Math.abs(anchor)*0.0004,0.01);
  let low=candleLow;
  let high=candleHigh;
  const expand=(raw)=>{
    const v=_ohlcNearTape(raw,anchor,candleSpan);
    if(v==null)return;
    low=Math.min(low,v);high=Math.max(high,v);
  };
  const zones=view.zones||{};
  const br=view.swing_brackets||{};
  const openEarly=(view.open_levels||[]).filter(p=>p&&(p.entry||p.stop||p.profit));
  const scaledInTrade=openEarly.length>0;
  // In a live trade, scale to candles + position levels only — prospective
  // swing/zone lines were collapsing GOLD into a hairline mess.
  if(!scaledInTrade){
    for(const z of [zones.support,zones.resistance,zones.buy_entry,zones.sell_entry]){
      if(!z)continue;
      expand(z.lo);expand(z.hi);
    }
    for(const k of ['stop_level','profit_level','swing_hi','swing_lo','kama']){
      expand(br[k]);
    }
  }
  for(const p of openEarly){
    expand(p.entry);expand(p.stop);expand(p.profit);
  }
  const ov=view.overlays||{};
  const sliceSeries=(arr)=>{
    if(!Array.isArray(arr)||!arr.length)return [];
    if(arr.length<=candles.length)return arr.slice();
    return arr.slice(-candles.length);
  };
  const kamaSeries=sliceSeries(ov.kama);
  const ema5Series=sliceSeries(ov.ema5);
  const ema13Series=sliceSeries(ov.ema13);
  // Overlay series may start with 0 warm-up — only expand near-tape points.
  for(const series of [kamaSeries,ema5Series,ema13Series]){
    for(const v of series)expand(v);
  }
  expand(mid);
  const atr=_ohlcFinite(ov.atr14)||_ohlcFinite((view.mtf||{}).atr14);
  if(atr!=null&&atr>0&&mid!=null){
    expand(mid-atr);expand(mid+atr);
  }

  // Hard clamp: never let overlays blow range past ~6× candle span from tape.
  const maxSpan=Math.max(candleSpan*6,Math.abs(anchor)*0.02,1);
  low=Math.max(low,anchor-maxSpan);
  high=Math.min(high,anchor+maxSpan);
  if(!(high>low)){low=anchor-candleSpan;high=anchor+candleSpan;}

  const pad=(high-low)*(showLabels?.05:.08);
  low-=pad;high+=pad;
  const yPadTop=showLabels?28:16;
  const yPadBot=showLabels?36:28;
  const leftPad=showLabels?18:12;
  const rightPad=showLabels?78:56;
  const chartW=Math.max(40,w-leftPad-rightPad);
  const y=p=>yPadTop+(high-p)/(high-low)*(h-yPadTop-yPadBot);

  if(showLabels){
    ctx.strokeStyle='rgba(26,37,52,.95)';
    ctx.lineWidth=1;
    for(let k=0;k<=4;k++){
      const yy=yPadTop+(h-yPadTop-yPadBot)*k/4;
      ctx.beginPath();ctx.moveTo(leftPad,yy);ctx.lineTo(leftPad+chartW,yy);ctx.stroke();
    }
  }

  const band=(z,fill,stroke)=>{
    if(!z)return;
    const a=_ohlcNearTape(z.lo,anchor,candleSpan);
    const b=_ohlcNearTape(z.hi,anchor,candleSpan);
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

  // Live position = truth. Hide prospective S/R + zones so entry/SL/TP
  // are not buried under overlapping swing/zone spaghetti.
  const openLv=(view.open_levels||[]).filter(p=>p&&(p.entry||p.stop||p.profit));
  const inTrade=openLv.length>0;
  if(!inTrade){
    band(zones.resistance,'rgba(255,92,122,.12)','rgba(255,92,122,.45)');
    band(zones.support,'rgba(57,255,136,.10)','rgba(57,255,136,.4)');
  }
  const pref=view.preferred;
  if(!inTrade){
    if(!pref||pref==='BUY')band(zones.buy_entry,'rgba(57,255,136,.28)',null);
    if(!pref||pref==='SELL')band(zones.sell_entry,'rgba(255,92,122,.26)',null);
  }

  if(atr!=null&&atr>0&&mid!=null&&_ohlcNearTape(mid,anchor,candleSpan)!=null){
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
      const v=_ohlcNearTape(series[i],anchor,candleSpan);
      if(v==null){started=false;continue;}
      const x=leftPad+(offset+i+.5)*spacing;
      if(!started){ctx.moveTo(x,y(v));started=true;}
      else ctx.lineTo(x,y(v));
    }
    if(started)ctx.stroke();
  };
  polyline(ema13Series,'rgba(140,160,180,.45)',1);
  polyline(ema5Series,'rgba(118,160,222,.75)',1.1);
  polyline(kamaSeries,'rgba(233,188,101,.95)',1.45);

  const bodyW=Math.max(showLabels?2.8:2.2,Math.min(showLabels?14:11,spacing*(showLabels?.72:.62)));
  candles.forEach((c,i)=>{
    const x=leftPad+(i+.5)*spacing;
    const rising=+c.close>=+c.open;
    const col=rising?'#31d59a':'#ef6875';
    ctx.strokeStyle=col;
    ctx.fillStyle=col;
    ctx.lineWidth=Math.max(1,Math.min(1.6,bodyW*.28));
    ctx.beginPath();ctx.moveTo(x,y(+c.high));ctx.lineTo(x,y(+c.low));ctx.stroke();
    const y1=y(+c.open),y2=y(+c.close);
    const bh=Math.max(1.5,Math.abs(y1-y2));
    ctx.fillRect(x-bodyW/2,Math.min(y1,y2),bodyW,bh);
  });

  const mark=(list,color)=>{
    if(!Array.isArray(list))return;
    ctx.fillStyle=color;
    for(const s of list){
      if(!_ohlcNearTape(s.price,anchor,candleSpan))continue;
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

  // Prospective swing brackets only when flat — never stack on live poz. levels.
  if(!inTrade&&br&&br.ready){
    hline(br.stop_level,'#ef6875',[4,3],'SL');
    hline(br.profit_level,'#31d59a',[4,3],'TP');
  }
  hline(mid,'#e9bc65',[2,2],'MID');
  for(const p of openLv){
    hline(p.entry,'#e9bc65',[],'ENT');
    hline(p.stop,'#ef6875',[4,3],'SL');
    hline(p.profit,'#31d59a',[4,3],'TP');
  }
  if(showLabels&&!inTrade){
    if(zones.buy_entry&&(!pref||pref==='BUY')){
      const z=zones.buy_entry;
      const a=_ohlcNearTape(z.lo,anchor,candleSpan);
      const b=_ohlcNearTape(z.hi,anchor,candleSpan)||a;
      if(a!=null){
        const midZ=(a+(b||a))/2;
        labels.push({y:y(midZ),color:'#39ff88',label:'BUY zona',price:midZ});
      }
    }
    if(zones.sell_entry&&(!pref||pref==='SELL')){
      const z=zones.sell_entry;
      const a=_ohlcNearTape(z.lo,anchor,candleSpan);
      const b=_ohlcNearTape(z.hi,anchor,candleSpan)||a;
      if(a!=null){
        const midZ=(a+(b||a))/2;
        labels.push({y:y(midZ),color:'#ff5c7a',label:'SELL zona',price:midZ});
      }
    }
  }

  const digits=Math.abs(high)<50?2:(Math.abs(high)<500?1:0);
  ctx.fillStyle='#8c95a6';
  ctx.font=(showLabels?'10px':'9px')+' "IBM Plex Mono",ui-monospace,monospace';
  ctx.textAlign='left';
  for(let k=0;k<=4;k++){
    const price=high-(high-low)*k/4;
    const yy=yPadTop+(h-yPadTop-yPadBot)*k/4;
    ctx.fillText(price.toFixed(digits),w-rightPad+6,yy+3);
  }

  if(showLabels&&labels.length){
    // Right-side SL / ENT / TP tags (PerpSignal style)
    labels.sort((a,b)=>a.y-b.y);
    let lastY=-999;
    ctx.font='700 9px "IBM Plex Mono",ui-monospace,monospace';
    for(const L of labels){
      let yy=L.y;
      if(yy-lastY<12)yy=lastY+12;
      lastY=yy;
      const text=`${L.label} ${L.price.toFixed(digits)}`;
      const tw=ctx.measureText(text).width+8;
      const tx=Math.max(leftPad+chartW-tw-2,leftPad+chartW*0.55);
      ctx.fillStyle='rgba(6,10,20,.88)';
      ctx.fillRect(tx-3,yy-9,tw,13);
      ctx.strokeStyle=L.color;ctx.lineWidth=1;
      ctx.strokeRect(tx-3,yy-9,tw,13);
      ctx.fillStyle=L.color;
      ctx.textAlign='left';
      ctx.fillText(text,tx,yy+1);
    }
  }

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
  // Use ?? so a real ADX/ATR of 0 is distinct from missing (null).
  // Do not redeclare `atr` — already bound above for the ATR band.
  const adx=_ohlcFinite(mtf.adx??ov.adx);
  const atrShow=_ohlcFinite(mtf.atr14??ov.atr14)??atr;
  if(adx!=null)parts.push(`ADX ${adx.toFixed(0)}`);
  if(atrShow!=null)parts.push(`ATR ${atrShow.toFixed(2)}`);
  if(kamaSeries.length||ov.kama_level!=null)parts.push('KAMA');
  if(view.info_stack)parts.push(String(view.info_stack));
  if(parts.length){
    ctx.fillStyle='rgba(140,149,166,.9)';
    ctx.font='8px "IBM Plex Mono",ui-monospace,monospace';
    ctx.textAlign='left';
    ctx.fillText(parts.join(' · '),leftPad,h-8);
  }
}

window.drawOhlcStructure=drawOhlcStructure;
