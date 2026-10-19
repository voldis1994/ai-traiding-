"""Legacy offline broker tests predate mandatory external-data guards.

Isolate the old broker/order/ML contracts from external network feeds. New
external-risk integration tests intentionally use the unmodified guards.
"""
import pytest

LEGACY_MODULES = {
    'test_adaptive_live_protection','test_integration','test_relative_edge_live',
    'test_system','test_validated_margin_unblock','test_multi_timeframe',
}

@pytest.fixture(autouse=True)
def legacy_offline_external_gate(request,monkeypatch):
    if request.module.__name__.split('.')[-1] not in LEGACY_MODULES:
        return
    from app.engine import SingleTradeEngine
    from app.intermarket import IntermarketMonitor
    async def test_only_calendar_refresh(self):
        self.last_calendar_refresh=1.0
        self.calendar_feed_error=None
    async def test_only_intermarket(self,broker,epic,instrument,main_rows,*,current=None,force=False):
        self.state={'status':'READY','entry_allowed':True,'reason':'Test fixture cross-market readiness',
                    'peers':[]}
        return self.state
    monkeypatch.setattr(SingleTradeEngine,'_refresh_calendar_if_due',test_only_calendar_refresh)
    monkeypatch.setattr(SingleTradeEngine,'_news_risk',lambda self: {
        'enabled':True,'entry_allowed':True,'status':'CLEAR','reason':'Test-only external safety fixture'})
    monkeypatch.setattr(IntermarketMonitor,'refresh',test_only_intermarket)
    monkeypatch.setattr(IntermarketMonitor,'assess',lambda self,action: {
        'entry_allowed':True,'status':'NO_CONFIRMED_CONTRADICTION','reason':'Test-only fixture'})
