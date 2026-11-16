"""Admin Signal Deck: lot / side / TP / trail truth on live deals."""
from app.runtime_hub import AccountRuntime


class _Store:
    def __init__(self):
        self.d = {}

    def get(self, key, default=None):
        return self.d.get(key, default)


class _Engine:
    def __init__(self):
        self.store = _Store()
        self.positions = []
        self.protection_brief = {}


def test_deal_truth_reports_tp_and_trail_phase():
    e = _Engine()
    p = {
        'dealId': 'd1', 'direction': 'SELL', 'size': '0.12', 'level': 100.,
        'stopLevel': 104., 'profitLevel': 96., 'upl': 0.3, 'epic': 'GOLD',
    }
    e.positions = [p]
    e.store.d['robot_open:d1'] = True
    e.store.d['adaptive_protection:d1'] = {
        'owner': 'AUTO', 'original_risk': 4.0, 'deal_id': 'd1'}
    e.store.d['adaptive_runtime:d1'] = {
        'status': 'CONFIRMED', 'phase': 'be_lock', 'stop': 100.0}
    e.protection_brief = {
        'status': 'Broker-confirmed trail update', 'deal_id': 'd1',
        'phase': 'be_lock'}
    t = AccountRuntime._deal_protection_truth(e, p)
    assert t['has_tp'] is True
    assert t['has_sl'] is True
    assert t['enrolled'] is True
    assert t['trail_phase'] == 'be_lock'


def test_enrich_positions_includes_lot_and_flags():
    e = _Engine()
    p = {
        'dealId': 'd2', 'direction': 'BUY', 'size': '0.15', 'level': 2000.,
        'stopLevel': 1995., 'profitLevel': None, 'upl': -0.1, 'epic': 'GOLD',
    }
    e.positions = [p]
    e.store.d['robot_open:d2'] = True
    # Not enrolled yet → watch
    rt = AccountRuntime.__new__(AccountRuntime)
    out = AccountRuntime._enrich_positions(rt, e, [p], brief={
        'status': 'Trail waiting for adaptive arm evidence', 'deal_id': 'd2'})
    assert len(out) == 1
    assert out[0]['size'] == '0.15'
    assert out[0]['direction'] == 'BUY'
    assert out[0]['has_tp'] is False
    assert out[0]['has_sl'] is True
    assert out[0]['trail_phase'] == 'watch'


def test_manual_position_not_marked_trailing():
    e = _Engine()
    p = {
        'dealId': 'm1', 'direction': 'BUY', 'size': '0.1', 'level': 10.,
        'stopLevel': 9., 'profitLevel': 11.,
    }
    e.positions = [p]
    t = AccountRuntime._deal_protection_truth(e, p)
    assert t['robot_open'] is False
    assert t['trail_phase'] == 'manual'
    assert t['has_tp'] is True
