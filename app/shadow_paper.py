"""Independent shadow paper trading — own position + cash-comparable PnL.

Never remaps LIVE broker £ onto the shadow vote. Each closed M1 bar the shadow
neural core votes BUY/SELL/HOLD; this journal opens/closes a virtual position
at mid ± half-spread.

Price-unit moves are converted to account-currency cash via a scale estimated
from recent LIVE broker trades (broker_£ / signed price move). Without that
scale, cash fields stay None — autonomy must not compare price units to £.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

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


def _close_price_pnl(direction, entry, exit_px):
    sign=1 if direction=='BUY' else -1
    return float(sign*(exit_px-entry))


def cash_per_price_unit(live_rows, *, min_samples=5):
    """Median broker-£ per unit price move from LIVE closed trades.

    Uses actual_pnl / signed(exit-entry). Captures size + FX into account
    currency without inventing tick_value. Returns None if under-powered.
    """
    ratios=[]
    for r in live_rows or []:
        pnl=_finite(r.get('actual_pnl'))
        entry=_finite(r.get('entry'));exit_px=_finite(r.get('exit'))
        direction=r.get('direction')
        if pnl is None or entry is None or exit_px is None:continue
        if direction not in ('BUY','SELL'):continue
        sign=1 if direction=='BUY' else -1
        move=sign*(exit_px-entry)
        if abs(move)<1e-12:continue
        ratios.append(pnl/move)
    if len(ratios)<int(min_samples):return None
    return float(np.median(ratios))


class ShadowPaperJournal:
    """Paper ledger for the shadow NeuralCore — never places LIVE orders."""

    def __init__(self, store):
        self.store=store
        self.history=list(self.store.get(STORE_KEY) or [])
        st=self.store.get(STATE_KEY) or {}
        self.position=st.get('position')  # {direction, entry, opened_at, bar_time}
        self.last_bar=st.get('last_bar')
        self.cash_scale=st.get('cash_scale')  # £ per price unit, or None

    def _persist(self):
        from .self_params import get as sp_get
        keep=int(sp_get(self.store,'profit_history_rows',_SEED_MAX_ROWS))
        self.store.set(STORE_KEY,self.history[-keep:])
        self.store.set(STATE_KEY,{
            'position':self.position,'last_bar':self.last_bar,
            'cash_scale':self.cash_scale})

    def update_cash_scale(self, live_rows, *, min_samples=5):
        """Refresh £/price-unit from LIVE broker history when possible."""
        scale=cash_per_price_unit(live_rows,min_samples=min_samples)
        if scale is not None and math.isfinite(scale):
            self.cash_scale=float(scale)
            self._persist()
        return self.cash_scale

    def on_bar(self, action, *, mid, spread=0., bar_time=None, cash_scale=None):
        """Advance paper book one closed bar. Returns closed row or None.

        Records price_pnl always; paper_pnl (cash) only when a scale is known.
        """
        mid=_finite(mid);sp=_finite(spread) or 0.
        if mid is None or mid<=0:return None
        if bar_time and bar_time==self.last_bar:return None
        self.last_bar=bar_time
        if cash_scale is not None:
            cs=_finite(cash_scale)
            if cs is not None:self.cash_scale=cs
        action=action if action in ('BUY','SELL','HOLD') else 'HOLD'
        closed=None
        pos=self.position
        if pos is not None:
            direction=pos.get('direction')
            # HOLD = keep the paper position (same idea as LIVE holding to SL/TP).
            # Closing on every HOLD made shadow ultra-high-frequency churn that
            # almost never beat LIVE OOS £ — evolution looked permanently blocked.
            if action in ('BUY','SELL') and action!=direction:
                exit_side='SELL' if direction=='BUY' else 'BUY'
                exit_px=_fill_price(exit_side,mid,sp)
                price_pnl=_close_price_pnl(direction,float(pos['entry']),exit_px)
                scale=_finite(self.cash_scale)
                cash=float(price_pnl*scale) if scale is not None else None
                closed={
                    'at':datetime.now(timezone.utc).isoformat(),
                    'bar_time':bar_time,
                    'direction':direction,
                    'entry':float(pos['entry']),'exit':exit_px,
                    'price_pnl':price_pnl,
                    # paper_pnl = account-currency cash when scale known; else None
                    'paper_pnl':cash,
                    'cash_scale':scale,
                    'unit':'account_currency' if cash is not None else 'price',
                    'spread':sp,
                    'exit_reason':'flip',
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

    def paper_cash_pnls(self):
        """Only cash-denominated rows — comparable to broker £."""
        return [float(r['paper_pnl']) for r in self.history
                if r.get('unit')=='account_currency'
                and r.get('paper_pnl') is not None
                and math.isfinite(float(r['paper_pnl']))]

    def paper_pnls(self):
        """Backward-compatible alias → cash-only (never mix price units)."""
        return self.paper_cash_pnls()

    def summary(self):
        pnls=self.paper_cash_pnls()
        return {
            'trades':len(self.history),
            'cash_trades':len(pnls),
            'open':self.position is not None,
            'position':self.position,
            'cash_scale':self.cash_scale,
            'paper_net':float(sum(pnls)) if pnls else 0.,
            'wins':sum(1 for x in pnls if x>0),
            'losses':sum(1 for x in pnls if x<0),
            'unit':'account_currency',
        }
