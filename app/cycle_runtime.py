"""Wire trade_cycle_fsm into LIVE / HOLD execution (audit item 1).

Engine calls these helpers at each real stage — not unit-test-only.
COMPLETE requires real VALIDATE proof (not a rubber-stamp).
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


def _validate_proof(cycle: dict, validate_proof: dict | None, *,
                    skipped: bool) -> tuple[bool, dict]:
    """Real VALIDATE — require deal identity + full teach/save evidence."""
    proof = dict(validate_proof or {})
    deal = str(cycle.get('deal_id') or '')
    ok = True
    reasons = []
    if not deal:
        ok = False
        reasons.append('missing_deal_id')
    proof_deal = str(proof.get('deal_id') or '')
    if deal and proof_deal and proof_deal != deal:
        ok = False
        reasons.append('deal_mismatch')
    if not skipped:
        if not proof.get('taught') and not proof.get('journal_persisted'):
            ok = False
            reasons.append('no_teach_or_persist_proof')
        src = str(proof.get('pnl_source') or '')
        if src and src not in ('broker', 'funds', 'cash_ledger', 'live_funds_mirror'):
            # Still settling — cannot COMPLETE.
            ok = False
            reasons.append('pnl_not_broker')
        # Full-cycle proofs required for LIVE teach COMPLETE.
        if proof.get('taught'):
            for key in ('learn_committed', 'teachers_settled',
                        'adaptive_done', 'bracket_done'):
                if not proof.get(key):
                    ok = False
                    reasons.append(f'missing_{key}')
            # weights_saved must be explicitly True (missing ≡ fail).
            if proof.get('weights_saved') is not True:
                ok = False
                reasons.append('weights_not_saved')
            if not proof.get('learn_committed'):
                ok = False
                reasons.append('learn_not_committed')
    else:
        # Skipped learn still needs settle proof when cash was expected.
        if proof.get('pending'):
            ok = False
            reasons.append('settlement_still_pending')
        # Actual skip reason required — never rubber-stamp empty skip.
        if not str(proof.get('skip_reason') or '').strip():
            ok = False
            reasons.append('missing_skip_reason')
    proof['ok'] = ok
    proof['reasons'] = reasons
    return ok, proof


def mark_learned(store, cycle: dict, *, model_version: str | None = None,
                 skipped: bool = False,
                 validate_proof: dict | None = None) -> dict:
    """LEARN then real VALIDATE — never rubber-stamp COMPLETE."""
    if skipped:
        cycle = fsm.finish_stage(
            cycle, 'LEARN', status=fsm.STATUS_SKIPPED, proof='no_teach')
    else:
        cycle = fsm.finish_stage(
            cycle, 'LEARN', result_version=model_version)
    cycle = fsm.start_stage(cycle, 'VALIDATE')
    ok, proof = _validate_proof(cycle, validate_proof, skipped=skipped)
    if ok:
        cycle = fsm.finish_stage(cycle, 'VALIDATE', proof=proof)
        cycle = fsm.mark_complete(cycle)
        telem.get_telemetry().note_cycle(complete=bool(cycle.get('complete')))
    else:
        cycle = fsm.finish_stage(
            cycle, 'VALIDATE', status=fsm.STATUS_FAILED,
            error_code='validate_failed', proof=proof)
        cycle = dict(cycle)
        cycle['complete'] = False
        telem.get_telemetry().note_cycle(complete=False)
    fsm.persist(store, cycle)
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
    """Load cycle for this deal only — never fall back to another trade's cycle."""
    ptr = store.get(f'trade_cycle:by_deal:{deal_id}')
    if not isinstance(ptr, dict) or not ptr.get('cycle_id'):
        return None
    cyc = fsm.load(store, str(ptr['cycle_id']))
    if not cyc:
        return None
    bound = str(cyc.get('deal_id') or '')
    if bound and bound != str(deal_id):
        return None
    return cyc
