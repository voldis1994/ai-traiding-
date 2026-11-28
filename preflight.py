"""Read-only LIVE account compatibility; never submit orders or print token."""
import argparse,json
from app.oanda import OandaLive,instrument_name

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--instrument',required=True);a=p.parse_args();instrument_name(a.instrument)
    c=OandaLive()
    try:
        s=c.account_summary();account=s['account'];available=a.instrument in c.instruments();trades=c.open_trades().get('trades',[])
        print(json.dumps(dict(environment='live',instrument=a.instrument,instrument_available=available,currency=account['currency'],open_trades=len(trades),last_transaction=s.get('lastTransactionID'),complete_candles=len(c.candles(a.instrument,100)) if available else 0,order_submission=False),indent=2))
    finally:c.close()
if __name__=='__main__':main()
