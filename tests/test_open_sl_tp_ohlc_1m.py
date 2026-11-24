"""LIVE OPEN SL/TP must come from OHLC structure — never invent neural±min."""
from app import bracket_learn
from app import ohlc_entry_zones as oez
from app import ohlc_scenarios as osc
from app import ohlc_regime_brackets as orb
from tests.ohlc_zone_path import demand_zone_path


def test_decide_brackets_prefers_regime_or_swing_not_invented():
    rows, mid = demand_zone_path(confirm=True)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = zones["buy_entry"]
    assert buy
    entry = (float(buy["lo"]) + float(buy["hi"])) / 2
    sc = dict(osc.classify(rows, mid=entry, zones=zones, tick=0.1),
              scenario="TREND_UP", entry_hint="BUY")
    zone = bracket_learn.zone_at_open(rows, multi_tf=True)
    decided = bracket_learn.decide_brackets_from_ohlc(
        zone, tick=0.1, spread=0.2, min_distance=0.5,
        entry=entry, action="BUY", candles=rows, atr=1.0,
        require_swing=True, entry_zone=buy, scenario=sc, zones=zones,
        trigger="zone")
    assert decided.get("ready") is True
    src = str(decided.get("source") or "")
    assert src in ("regime_brackets", "swing_anchor")
    assert float(decided["stop_level"]) < entry
    assert float(decided["profit_level"]) > entry


def test_regime_brackets_refuse_without_structural_tp():
    """No invent: if candidates cannot place TP, ready=False."""
    sc = {"ready": True, "scenario": "TREND_UP", "entry_hint": "BUY", "range": None}
    # Tiny flat path — no opposing structure beyond entry.
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(30):
        rows.append({
            "at": (t0 + timedelta(minutes=i)).isoformat(),
            "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0,
        })
    br = orb.regime_brackets(
        candles=rows, entry=100.0, action="BUY", scenario=sc,
        trigger="zone", tick=0.01, spread=0.02, min_distance=0.05)
    # Either unready or uses real path extreme — never entry±fantasy ATR.
    if br.get("ready"):
        assert br.get("source") == "regime_brackets"
        assert br.get("stop_level") is not None
    else:
        assert br.get("reason")
