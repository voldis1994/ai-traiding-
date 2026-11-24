"""Wire trade_cycle_fsm into LIVE / HOLD execution (audit item 1).

Engine calls these helpers at each real stage — not unit-test-only.
"""
from __future__ import annotations

from typing import Any

from . import trade_cycle_fsm as fsm
from . import telemetry as telem


def begin_analysis_cycle(store, *, account_id: str | None = None) -> dict[str, Any]:
    cycle = fsm.new_cycle(account_id=account_id)
    cycle = fsm.start_stage(cycle, 'PERCEIVE')
    fsm.persist(store, cycle)
    return cycle


def mark_perceived(store, cycle: dict, *, snapshot_id: str | None = None,
                   candle_at: str | None = None) -> dict:
    cycle = fsm.finish_stage(
        cycle, 'PERCEIVE', proof={
            'snapshot_id': snapshot_id, 'candle_at': candle_at})
    cycle = fsm.start_stage(cycle, 'ANALYZE', input_version=snapshot_id)
    fsm.persist(store, cycle)
    st = (cycle.get('stages') or {}).get('PERCEIVE') or {}
    telem.get_telemetry().note_stage('PERCEIVE', st.get('duration_ms'))
    return cycle


def mark_analyzed(store, cycle: dict, *, analysis_version: str | None = None) -> dict:
    cycle = fsm.finish_stage(
        cycle, 'ANALYZE', result_version=analysis_version)
    cycle = fsm.start_stage(cycle, 'DECIDE', input_version=analysis_version)
    fsm.persist(store, cycle)
    st = (cycle.get('stages') or {}).get('ANALYZE') or {}
    telem.get_telemetry().note_stage('ANALYZE', st.get('duration_ms'))
    return cycle


def decide(store, cycle: dict, action: str, *, reason: str | None = None) -> dict:
    """BUY/SELL continues; HOLD completes analysis cycle cleanly."""
    action_u = str(action or 'HOLD').upper()
    if action_u == 'HOLD':
        cycle = fsm.set_decision(cycle, 'HOLD', reason=reason)
        fsm.persist(store, cycle)
        telem.get_telemetry().note_cycle(complete=True)
        return cycle
    cycle = fsm.set_decision(cycle, action_u, reason=reason)
    # set_decision for non-HOLD does not skip execute path
    cycle = fsm.finish_stage(cycle, 'DECIDE', proof=reason or action_u)
    cycle = fsm.start_stage(cycle, 'EXECUTE')
    fsm.persist(store, cycle)
    return cycle


def mark_executed(store, cycle: dict, *, deal_id: str | None = None,
                  deal_ref: str | None = None) -> dict:
    if deal_id:
        cycle = dict(cycle)
        cycle['deal_id'] = str(deal_id)
    cycle = fsm.finish_stage(
        cycle, 'EXECUTE',
        proof={'deal_id': deal_id, 'dealReference': deal_ref})
    cycle = fsm.start_stage(cycle, 'MANAGE')
    fsm.persist(store, cycle)
    st = (cycle.get('stages') or {}).get('EXECUTE') or {}
    telem.get_telemetry().note_stage('EXECUTE', st.get('duration_ms'))
    return cycle


def mark_manage_tick(store, cycle: dict, *, upl: float | None = None) -> dict:
    # MANAGE stays RUNNING until close — refresh proof only.
    stages = dict(cycle.get('stages') or {})
    st = dict(stages.get('MANAGE') or fsm._blank_stage())
    st['status'] = fsm.STATUS_RUNNING
    st['proof'] = {'upl': upl}
    stages['MANAGE'] = st
    cycle = dict(cycle)
    cycle['stages'] = stages
    fsm.persist(store, cycle)
    return cycle


def mark_closed(store, cycle: dict, *, close_proof: Any = None) -> dict:
    cycle = fsm.finish_stage(cycle, 'MANAGE')
    cycle = fsm.start_stage(cycle, 'CLOSE')
    cycle = fsm.finish_stage(cycle, 'CLOSE', proof=close_proof)
    cycle = fsm.start_stage(cycle, 'SETTLE')
    fsm.persist(store, cycle)
    return cycle


def mark_settled(store, cycle: dict, *, funds_proof: Any = None,
                 pending: bool = False) -> dict:
    if pending:
        cycle = fsm.mark_settlement_pending(cycle)
        fsm.persist(store, cycle)
        telem.get_telemetry().note_cycle(complete=False)
        return cycle
    cycle = fsm.finish_stage(cycle, 'SETTLE', proof=funds_proof)
    cycle = fsm.start_stage(cycle, 'LEARN')
    fsm.persist(store, cycle)
    return cycle


def mark_learned(store, cycle: dict, *, model_version: str | None = None,
                 skipped: bool = False) -> dict:
    if skipped:
        cycle = fsm.finish_stage(
            cycle, 'LEARN', status=fsm.STATUS_SKIPPED, proof='no_teach')
    else:
        cycle = fsm.finish_stage(
            cycle, 'LEARN', result_version=model_version)
    cycle = fsm.start_stage(cycle, 'VALIDATE')
    cycle = fsm.finish_stage(cycle, 'VALIDATE', proof='ok')
    cycle = fsm.mark_complete(cycle)
    fsm.persist(store, cycle)
    telem.get_telemetry().note_cycle(complete=bool(cycle.get('complete')))
    st = (cycle.get('stages') or {}).get('LEARN') or {}
    telem.get_telemetry().note_stage('LEARN', st.get('duration_ms'))
    return cycle


def active_cycle(store) -> dict | None:
    return fsm.load_latest(store)


def bind_deal(store, cycle: dict, deal_id: str) -> dict:
    cycle = dict(cycle)
    cycle['deal_id'] = str(deal_id)
    fsm.persist(store, cycle)
    return cycle


def load_by_deal(store, deal_id: str) -> dict | None:
    ptr = store.get(f'trade_cycle:by_deal:{deal_id}')
    if not isinstance(ptr, dict) or not ptr.get('cycle_id'):
        return None
    return fsm.load(store, str(ptr['cycle_id']))
