"""Standalone OANDA LIVE read-only latency monitor. No orders, no neural model."""
import argparse, time, sys, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from app.oanda import OandaLive,decode_price,instrument_name
from app.market_clock import utc
from app.latency_monitor import LatencyMonitor,stats


def rest_probe(broker,instrument,seconds,out,interval=1.):
    seen=set();baseline=False;values=[];deadline=time.monotonic()+seconds
    Path(out).parent.mkdir(parents=True,exist_ok=True)
    with Path(out).open('x',encoding='utf-8') as f:
        while time.monotonic()<deadline:
            start=time.monotonic();rows=broker.candles(instrument,3);received=datetime.now(timezone.utc)
            for row in rows:
                end=utc(row['time'])+timedelta(minutes=1);key=end.isoformat()
                if key in seen:continue
                seen.add(key)
                if not baseline:continue
                age=(received-end).total_seconds()*1000;values.append(age)
                line=dict(candle_end=key,first_observed=received.isoformat(),observed_delay_ms=age,request_ms=(time.monotonic()-start)*1000)
                f.write(json.dumps(line)+'\n');f.flush();print(json.dumps(line),flush=True)
            baseline=True
            time.sleep(min(interval,max(0,deadline-time.monotonic())))
    print(json.dumps({'rest_first_observed_ms':stats(values),'poll_interval_seconds':interval,'note':'Upper bound, includes polling and HTTP delay; not exact publication time.'}),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--instrument',required=True);p.add_argument('--seconds',type=int,default=259200);p.add_argument('--out',default='latency.json');p.add_argument('--rest-probe',action='store_true');a=p.parse_args()
    if a.seconds<=0:p.error('Positive duration required')
    broker=OandaLive();m=None
    try:
        instrument=instrument_name(a.instrument)
        if instrument not in broker.instruments():raise ValueError('Instrument unavailable')
        if a.rest_probe:rest_probe(broker,instrument,a.seconds,a.out);return
        m=LatencyMonitor(a.out);deadline=time.monotonic()+a.seconds
        for row in broker.messages(instrument):
            if time.monotonic()>=deadline:break
            raw=row['message']
            if raw.get('type')=='HEARTBEAT':m.heartbeat(row['received_at']);continue
            try:q=decode_price(raw,row['received_at'])
            except (ValueError,KeyError,TypeError,OverflowError):q=None
            if q is None:m.invalid()
            else:m.quote(q)
    finally:
        if m:m.finish()
        broker.close()

if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:pass
    except Exception as e:print('Latency monitor stopped: '+type(e).__name__,file=sys.stderr);sys.exit(1)
