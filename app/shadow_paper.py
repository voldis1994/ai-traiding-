"""Independent shadow paper trading — own position + PnL journal.

Never remaps LIVE broker £ onto the shadow vote. Each closed M1 bar the shadow
neural core votes BUY/SELL/HOLD; this journal opens/closes a virtual position
at mid ± half-spread and records paper £ independently of the live robot.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

STORE_KEY = 'shadow_paper_journal_v1'
STATE_KEY = 'shadow_paper_state_v1'
_SEED_MAX_ROWS = 400


def _finite(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (TypeError,ValueError):
        return None


def _fill_price(direction, mid, spread):
    """Conservative fill: BUY pays ask, SELL hits bid (half-spread each side)."""
    half=abs(float(spread or 0))/2.
    if direction=='BUY':
        return float(mid)+half
    return float(mid)-half


def _close_pnl(direction, entry, exit_px):
    sign=1 if direction=='BUY' else -1
    return float(sign*(exit_px-entry))


class ShadowPaperJournal:
    """Paper ledger for the shadow NeuralCore — never places LIVE orders."""

    def __init__(self, store):
        self.store=store
        self.history=list(self.store.get(STORE_KEY) or [])
        st=self.store.get(STATE_KEY) or {}
        self.position=st.get('position')  # {direction, entry, opened_at, bar_time}
        self.last_bar=st.get('last_bar')

    def _persist(self):
        from .self_params import get as sp_get
        keep=int(sp_get(self.store,'profit_history_rows',_SEED_MAX_ROWS))
        self.store.set(STORE_KEY,self.history[-keep:])
        self.store.set(STATE_KEY,{
            'position':self.position,'last_bar':self.last_bar})

    def on_bar(self, action, *, mid, spread=0., bar_time=None):
        """Advance paper book one closed bar. Returns closed row or None."""
        mid=_finite(mid);sp=_finite(spread) or 0.
        if mid is None or mid<=0:return None
        if bar_time and bar_time==self.last_bar:return None
        self.last_bar=bar_time
        action=action if action in ('BUY','SELL','HOLD') else 'HOLD'
        closed=None
        pos=self.position
        if pos is not None:
            direction=pos.get('direction')
            # Close on HOLD or opposite vote — independent of LIVE.
            if action=='HOLD' or (action in ('BUY','SELL') and action!=direction):
                # Flatten at the exit side of the spread (independent of LIVE).
                exit_side='SELL' if direction=='BUY' else 'BUY'
                exit_px=_fill_price(exit_side,mid,sp)
                pnl=_close_pnl(direction,float(pos['entry']),exit_px)
                closed={
                    'at':datetime.now(timezone.utc).isoformat(),
                    'bar_time':bar_time,
                    'direction':direction,
                    'entry':float(pos['entry']),'exit':exit_px,
                    'paper_pnl':pnl,'spread':sp,
                    'exit_reason':'hold' if action=='HOLD' else 'flip',
                    'source':'shadow_paper'}
                self.history.append(closed)
                self.position=None
        if self.position is None and action in ('BUY','SELL'):
            entry=_fill_price(action,mid,sp)
            self.position={
                'direction':action,'entry':entry,
                'opened_at':datetime.now(timezone.utc).isoformat(),
                'bar_time':bar_time}
        self._persist()
        return closed

    def paper_pnls(self):
        return [float(r['paper_pnl']) for r in self.history
                if r.get('paper_pnl') is not None and math.isfinite(float(r['paper_pnl']))]

    def summary(self):
        pnls=self.paper_pnls()
        return {
            'trades':len(self.history),
            'open':self.position is not None,
            'position':self.position,
            'paper_net':float(sum(pnls)) if pnls else 0.,
            'wins':sum(1 for x in pnls if x>0),
            'losses':sum(1 for x in pnls if x<0),
        }
