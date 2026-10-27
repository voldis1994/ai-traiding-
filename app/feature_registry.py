"""Dynamic feature registry — base OHLC + approved discoveries + external fusion.

Capital.com does not expose true volume, DOM, or order-flow. Discovery may only
append formulas over completed OHLC, spread, peers, and scheduled news.
Approved extras expand neural input dim without rewriting base names.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .market_context import FEATURE_NAMES as BASE_OHLC_NAMES, FEATURE_SCHEMA as BASE_OHLC_SCHEMA
from .market_fusion import EXTERNAL_NAMES

STORE_KEY = 'feature_registry_v1'
# Seed only — live cap is self_params.max_approved_features (robot-owned).
MAX_APPROVED = 32


def _digest(names):
    raw=','.join(names).encode()
    return hashlib.sha256(raw).hexdigest()[:12]


class FeatureRegistry:
    """Persisted list of independently validated dynamic feature specs."""

    def __init__(self, store=None):
        self.store=store
        self.approved=[]  # [{id,name,formula,source,approved_at,metrics}, ...]
        if store is not None:
            self.load()

    def _cap(self):
        if self.store is None:return MAX_APPROVED
        from .self_params import get as sp_get
        return int(sp_get(self.store,'max_approved_features',MAX_APPROVED))

    def load(self):
        raw=self.store.get(STORE_KEY) if self.store else None
        if isinstance(raw,dict) and isinstance(raw.get('approved'),list):
            self.approved=[x for x in raw['approved'] if isinstance(x,dict) and x.get('id') and x.get('name')]
            self.approved=self.approved[:self._cap()]
        else:
            self.approved=[]

    def save(self):
        if self.store is None:return
        self.store.set(STORE_KEY,{
            'approved':self.approved,
            'ohlc_schema':self.ohlc_schema(),
            'fusion_schema':self.fusion_schema(),
            'ohlc_dim':self.ohlc_dim(),
            'fusion_dim':self.fusion_dim()})

    def approved_ids(self):
        return [str(x['id']) for x in self.approved]

    def approved_names(self):
        return [str(x['name']) for x in self.approved]

    def ohlc_names(self):
        return list(BASE_OHLC_NAMES)+self.approved_names()

    def fusion_names(self):
        return self.ohlc_names()+list(EXTERNAL_NAMES)

    def ohlc_dim(self):
        return len(self.ohlc_names())

    def fusion_dim(self):
        return len(self.fusion_names())

    def base_ohlc_dim(self):
        return len(BASE_OHLC_NAMES)

    def ohlc_schema(self):
        if not self.approved:
            return BASE_OHLC_SCHEMA
        return f'{BASE_OHLC_SCHEMA}+dyn_{_digest(self.approved_ids())}'

    def fusion_schema(self):
        base=f'{self.ohlc_schema()}_intermarket_news'
        return base if self.approved else 'closed_m1_m5_m15_h1_intermarket_news_shadow_v5'

    def has(self, feature_id):
        return feature_id in self.approved_ids()

    def approve(self, spec: dict[str, Any]):
        """Append one validated feature; reject duplicates / over-cap."""
        fid=str(spec.get('id') or '')
        name=str(spec.get('name') or '')
        if not fid or not name:return False,'missing id/name'
        if self.has(fid) or name in self.approved_names() or name in BASE_OHLC_NAMES:
            return False,'duplicate'
        if name in EXTERNAL_NAMES:return False,'collides with external'
        if len(self.approved)>=self._cap():return False,'cap reached'
        entry={
            'id':fid,'name':name,
            'formula':str(spec.get('formula') or ''),
            'source':str(spec.get('source') or 'discovery'),
            'approved_at':spec.get('approved_at'),
            'metrics':spec.get('metrics') or {}}
        self.approved.append(entry)
        self.save()
        return True,'approved'

    def reject_remove(self, feature_id):
        before=len(self.approved)
        self.approved=[x for x in self.approved if x.get('id')!=feature_id]
        if len(self.approved)==before:return False
        self.save()
        return True

    def replace_approved(self, specs):
        """Replace the dynamic feature list (rollback to a prior snapshot).

        Used with neural weight restore so input layout matches the backup.
        """
        out=[]
        for x in list(specs or []):
            if not isinstance(x,dict):continue
            fid=str(x.get('id') or '');name=str(x.get('name') or '')
            if not fid or not name:continue
            if name in BASE_OHLC_NAMES or name in EXTERNAL_NAMES:continue
            if any(e['id']==fid or e['name']==name for e in out):continue
            out.append({
                'id':fid,'name':name,
                'formula':str(x.get('formula') or ''),
                'source':str(x.get('source') or 'rollback'),
                'approved_at':x.get('approved_at'),
                'metrics':x.get('metrics') or {}})
            if len(out)>=self._cap():break
        self.approved=out
        self.save()
        return list(self.approved)

    def snapshot(self):
        return {
            'base_ohlc':list(BASE_OHLC_NAMES),
            'approved':list(self.approved),
            'external':list(EXTERNAL_NAMES),
            'ohlc_dim':self.ohlc_dim(),
            'fusion_dim':self.fusion_dim(),
            'ohlc_schema':self.ohlc_schema(),
            'fusion_schema':self.fusion_schema()}


def registry_from_store(store):
    return FeatureRegistry(store)
