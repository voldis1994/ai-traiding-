"""Read-only rolling latency measurements. Not an exchange latency guarantee."""
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json, math, os, time, html
from .market_clock import utc, minute


def stats(values):
    a=sorted(values)
    if not a:return None
    def percentile(p):return round(a[max(0,math.ceil(p*len(a))-1)],3)
    return dict(n=len(a),p50=percentile(.5),p95=percentile(.95),p99=percentile(.99),maximum=round(a[-1],3))


class LatencyMonitor:
    def __init__(self,path,*,window=10000,interval=5,clock=None,monotonic=None,emit=print):
        if window<1 or interval<=0:raise ValueError('Invalid monitor settings')
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.clock=clock or (lambda:datetime.now(timezone.utc));self.mono=monotonic or time.monotonic
        self.emit=emit;self.interval=interval;self.window=window
        self.samples={k:deque(maxlen=window) for k in ('wire_ms','queue_ms','available_ms','event_gap_ms','minute_boundary_ms','processing_ms')}
        self.start=self.last_report=self.mono();self.last_event=None;self.last_wire=None;self.last_bucket=None
        self.count=self.invalid_count=self.heartbeats=self.clock_skew=self.out_of_order=self.gaps=0
        self.last_message=None;self.instrument=None
        self.publish()

    def quote(self,q):
        wire=q.wire_received_at or q.received_at
        age=(wire-q.event_time).total_seconds()*1000
        queue=(q.received_at-wire).total_seconds()*1000
        self.instrument=q.instrument;self.count+=1
        self.samples['wire_ms'].append(age);self.samples['queue_ms'].append(queue);self.samples['available_ms'].append(q.latency*1000)
        self.clock_skew+=int(age<0)
        if self.last_event is not None:
            gap=(q.event_time-self.last_event).total_seconds()*1000
            if gap>0:
                self.samples['event_gap_ms'].append(gap);self.gaps+=int(gap>10000)
            elif gap<0:self.out_of_order+=1
        bucket=minute(q.event_time)
        if self.last_bucket is not None and bucket==self.last_bucket+timedelta(minutes=1):
            self.samples['minute_boundary_ms'].append((wire-bucket).total_seconds()*1000)
        if self.last_event is None or q.event_time>self.last_event:
            self.last_event=q.event_time;self.last_bucket=bucket
        self.last_wire=wire;self.last_message=wire
        self.maybe_publish()

    def processing(self,seconds):self.samples['processing_ms'].append(seconds*1000)
    def heartbeat(self,received):
        self.heartbeats+=1;self.last_message=utc(received);self.maybe_publish()
    def invalid(self):
        self.invalid_count+=1;self.maybe_publish()

    def snapshot(self,*,finished=False):
        now=utc(self.clock());elapsed=max(self.mono()-self.start,0.)
        return dict(updated_utc=now.isoformat(),finished=finished,instrument=self.instrument,elapsed_seconds=round(elapsed,3),
                    quotes=self.count,quotes_per_second=round(self.count/elapsed,3) if elapsed else None,
                    window=self.window,heartbeats=self.heartbeats,invalid_messages=self.invalid_count,negative_wire_age=self.clock_skew,
                    out_of_order=self.out_of_order,event_gaps_over_10s=self.gaps,
                    seconds_since_last_price=(now-self.last_wire).total_seconds() if self.last_wire else None,
                    seconds_since_last_message=(now-self.last_message).total_seconds() if self.last_message else None,
                    metrics={k:stats(v) for k,v in self.samples.items()},latest_ms={k:round(v[-1],3) if v else None for k,v in self.samples.items()},
                    note='Wire age includes broker timestamp and local clock error. Minute boundary is first quote in next minute, not REST candle availability.')

    def publish(self,*,finished=False):
        data=self.snapshot(finished=finished);tmp=self.path.with_name(self.path.name+'.tmp')
        tmp.write_text(json.dumps(data,indent=2,allow_nan=False),encoding='utf-8');os.replace(tmp,self.path)
        page=self.path.with_suffix('.html');page_tmp=page.with_name(page.name+'.tmp')
        page_tmp.write_text(dashboard(data),encoding='utf-8');os.replace(page_tmp,page)
        with self.path.with_suffix('.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(data,allow_nan=False)+'\n')
        self.last_report=self.mono()
        if self.emit:self.emit(json.dumps(data,separators=(',',':')),flush=True)

    def maybe_publish(self):
        if self.mono()-self.last_report>=self.interval:self.publish()
    def finish(self):self.publish(finished=True)


def dashboard(data):
    names={'wire_ms':'Brokera laiks → saņemšana','queue_ms':'Robota rindas kavējums','available_ms':'Kopējais datu vecums','event_gap_ms':'Intervāls starp kotējumiem','minute_boundary_ms':'Pirmā kotācija pēc M1 robežas','processing_ms':'Robota apstrādes laiks'}
    def fmt(x):return '—' if x is None else f'{x:.2f}'
    rows=[]
    for key,name in names.items():
        v=data['metrics'][key] or {};values=[data['latest_ms'][key],v.get('p50'),v.get('p95'),v.get('p99'),v.get('maximum')]
        rows.append('<tr><th>'+html.escape(name)+'</th>'+''.join('<td>'+fmt(x)+'</td>' for x in values)+'</tr>')
    return ('<!doctype html><html lang="lv"><meta charset="utf-8"><meta http-equiv="refresh" content="5"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>OANDA LIVE — datu monitors</title><style>body{font:16px system-ui;color:#182b3d;background:#f4f7fa;max-width:1000px;margin:32px auto;padding:0 16px}h1{font-size:26px}section,table{background:white;border:1px solid #d7e0e9;border-radius:8px;padding:16px;margin:16px 0}table{width:100%;border-collapse:collapse}td,th{text-align:right;padding:10px;border-bottom:1px solid #e2e8f0}th:first-child{text-align:left}small{color:#42566b}</style>'
            '<h1>OANDA LIVE — datu monitors</h1><section><b>'+html.escape(str(data['instrument'] or 'Gaida datus'))+'</b> · '+('Sesija pabeigta' if data['finished'] else 'Pēdējais mērījums')+' · '+html.escape(data['updated_utc'])+'<p>Kotējumi: '+str(data['quotes'])+' · kotējumi/s: '+fmt(data['quotes_per_second'])+' · heartbeat: '+str(data['heartbeats'])+' · nederīgi: '+str(data['invalid_messages'])+'</p><p>Vecums pēdējā mērījuma brīdī (sek.): '+fmt(data['seconds_since_last_price'])+' · pārrāvumi &gt;10 s: '+str(data['event_gaps_over_10s'])+' · negatīvs datu vecums: '+str(data['negative_wire_age'])+'</p></section><table><thead><tr><th>Mērījums (ms)</th><th>Pēdējais</th><th>P50</th><th>P95</th><th>P99</th><th>Max</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table><small>Slīdošais logs: '+str(data['window'])+' mērījumi. Ja laika zīmogs vairs nemainās, monitors nesaņem jaunus ziņojumus. Precīzs datu vecums atkarīgs no datora UTC pulksteņa. M1 robežas mērījums nav REST sveces publikācijas laiks. — = nav mērījumu.</small></html>')
