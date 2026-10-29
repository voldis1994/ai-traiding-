"""Automatic policy Python source generation, isolated smoke-test and promotion.

This is code generation (not just weight tuning). Code changes are constrained to
one well-defined strategy module: generated code never receives broker credentials,
filesystem or network privileges, and cannot change the order router.
"""
import ast
import hashlib
import json
import math
from datetime import datetime, timezone
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import numpy as np
from .storage import DATA

ROOT=Path(__file__).resolve().parent.parent
# Legacy default only — prefer store.path.parent/'policy.py' (hub always passes path).
POLICY=DATA/'active_policy.py'

# Whitelisted, explicit policy shapes. Each candidate is a whole Python module.
# The generator can evolve both the function's branching structure and constants.
TEMPLATES={
'confidence':'''# Auto-evolved source: directional confidence model
CONFIDENCE = {confidence:.2f}
REVERSAL_CONFIDENCE = {reversal:.2f}
MIN_HOLD_TICKS = {min_hold}

def decide(scores, current_direction=None, age=0):
    action = max(scores, key=scores.get)
    if action == 'HOLD' or scores[action] < CONFIDENCE:
        return 'HOLD'
    if current_direction is None:
        return action
    if age < MIN_HOLD_TICKS or current_direction == action:
        return 'HOLD'
    return 'CLOSE' if scores[action] >= REVERSAL_CONFIDENCE else 'HOLD'
''',
'margin':'''# Auto-evolved source: winner-vs-runner-up margin model
CONFIDENCE = {confidence:.2f}
REVERSAL_CONFIDENCE = {reversal:.2f}
MIN_HOLD_TICKS = {min_hold}
MIN_MARGIN = {margin:.2f}

def decide(scores, current_direction=None, age=0):
    ranking = sorted(scores, key=scores.get, reverse=True)
    action = ranking[0]
    runner = ranking[1]
    if action == 'HOLD':
        return 'HOLD'
    if scores[action] < CONFIDENCE or scores[action] - scores[runner] < MIN_MARGIN:
        return 'HOLD'
    if current_direction is None:
        return action
    if current_direction != action and age >= MIN_HOLD_TICKS and scores[action] >= REVERSAL_CONFIDENCE:
        return 'CLOSE'
    return 'HOLD'
''',
'hold_bias':'''# Auto-evolved source: explicit HOLD competition model
CONFIDENCE = {confidence:.2f}
REVERSAL_CONFIDENCE = {reversal:.2f}
MIN_HOLD_TICKS = {min_hold}
HOLD_BIAS = {margin:.2f}

def decide(scores, current_direction=None, age=0):
    buy = scores['BUY']
    sell = scores['SELL']
    hold = scores['HOLD'] + HOLD_BIAS
    if buy <= hold and sell <= hold:
        return 'HOLD'
    action = 'BUY' if buy >= sell else 'SELL'
    if scores[action] < CONFIDENCE:
        return 'HOLD'
    if current_direction is None:
        return action
    if action == current_direction or age < MIN_HOLD_TICKS:
        return 'HOLD'
    return 'CLOSE' if scores[action] >= REVERSAL_CONFIDENCE else 'HOLD'
'''
}

# Additional bounded, interpretable decision structures for shadow replay.
TEMPLATES['directional_margin'] = '''# Directional competition against HOLD and opposite side
CONFIDENCE = {confidence:.2f}
REVERSAL_CONFIDENCE = {reversal:.2f}
MIN_HOLD_TICKS = {min_hold}
MIN_MARGIN = {margin:.2f}

def decide(scores, current_direction=None, age=0):
    action = 'BUY' if scores['BUY'] >= scores['SELL'] else 'SELL'
    if scores[action] < CONFIDENCE or scores[action] - max(scores['HOLD'], scores['SELL' if action == 'BUY' else 'BUY']) < MIN_MARGIN:
        return 'HOLD'
    if current_direction is None:
        return action
    if current_direction != action and age >= MIN_HOLD_TICKS and scores[action] >= REVERSAL_CONFIDENCE:
        return 'CLOSE'
    return 'HOLD'
'''
TEMPLATES['relative_hold'] = '''# Require directional evidence above HOLD with configurable cushion
CONFIDENCE = {confidence:.2f}
REVERSAL_CONFIDENCE = {reversal:.2f}
MIN_HOLD_TICKS = {min_hold}
HOLD_BIAS = {margin:.2f}

def decide(scores, current_direction=None, age=0):
    action = 'BUY' if scores['BUY'] >= scores['SELL'] else 'SELL'
    if scores[action] < CONFIDENCE or scores[action] <= scores['HOLD'] + HOLD_BIAS:
        return 'HOLD'
    if current_direction is None:
        return action
    if current_direction != action and age >= MIN_HOLD_TICKS and scores[action] >= REVERSAL_CONFIDENCE:
        return 'CLOSE'
    return 'HOLD'
'''

# V2: work in score DIFFERENCES, not uncalibrated class probabilities.
# A 43% SELL is not inherently weaker than a 60% absolute threshold if
# the BUY and HOLD alternatives score significantly lower.
TEMPLATES['edge'] = '''# Auto-evolved source: directional evidence, policy v2
MIN_EDGE = {margin:.2f}
REVERSAL_EDGE = {reversal:.2f}
MIN_HOLD_TICKS = {min_hold}

def decide(scores, current_direction=None, age=0):
    action = 'BUY' if scores['BUY'] >= scores['SELL'] else 'SELL'
    runner = max(scores['HOLD'], scores['SELL' if action == 'BUY' else 'BUY'])
    if scores[action] - runner < MIN_EDGE:
        return 'HOLD'
    if current_direction is None:
        return action
    if current_direction == action or age < MIN_HOLD_TICKS:
        return 'HOLD'
    return 'CLOSE' if scores[action] - runner >= REVERSAL_EDGE else 'HOLD'
'''
TEMPLATES['edge_vs_hold'] = '''# Auto-evolved source: directional advantage over both alternatives, policy v2
MIN_EDGE = {margin:.2f}
REVERSAL_EDGE = {reversal:.2f}
MIN_HOLD_TICKS = {min_hold}

def decide(scores, current_direction=None, age=0):
    ranking = sorted(scores, key=scores.get, reverse=True)
    action = ranking[0]
    if action == 'HOLD' or scores[action] - scores[ranking[1]] < MIN_EDGE:
        return 'HOLD'
    if current_direction is None:
        return action
    if current_direction == action or age < MIN_HOLD_TICKS:
        return 'HOLD'
    return 'CLOSE' if scores[action] - scores[ranking[1]] >= REVERSAL_EDGE else 'HOLD'
'''
TEMPLATES['edge_hold_cushion'] = '''# Auto-evolved source: explicit HOLD challenger, policy v2
MIN_EDGE = {margin:.2f}
REVERSAL_EDGE = {reversal:.2f}
MIN_HOLD_TICKS = {min_hold}

def decide(scores, current_direction=None, age=0):
    action = 'BUY' if scores['BUY'] >= scores['SELL'] else 'SELL'
    opposite = 'SELL' if action == 'BUY' else 'BUY'
    if scores[action] - scores[opposite] < MIN_EDGE or scores[action] - scores['HOLD'] < MIN_EDGE:
        return 'HOLD'
    if current_direction is None:
        return action
    if current_direction == action or age < MIN_HOLD_TICKS:
        return 'HOLD'
    return 'CLOSE' if scores[action] - max(scores[opposite],scores['HOLD']) >= REVERSAL_EDGE else 'HOLD'
'''

DEFAULT=TEMPLATES['edge'].format(confidence=.60,reversal=.30,min_hold=3,margin=.06)
EDGE_POLICY_KINDS=('edge','edge_vs_hold','edge_hold_cushion')


class AutoEvolution:
    def __init__(self,store,path=None):
        # Bind policy file to the same directory as the robot journal when possible
        # so multi-tenant clients never share DATA/active_policy.py by accident.
        if path is None:
            parent=Path(store.path).parent if hasattr(store,'path') and store.path else DATA
            path=parent/'policy.py' if parent!=DATA else POLICY
        self.store=store;self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        if not self.path.exists():self.path.write_text(DEFAULT,encoding='utf8')
        # Keep the old evolved module for audit/rollback; it must not secretly
        # retain the absolute 60% entry rule after a source-code upgrade.
        # Never overwrite an unrecognized operator-authored policy.
        original=self.path.read_text(encoding='utf8')
        self.migrated=False
        if 'MIN_EDGE' not in original and (original.startswith('# Auto-evolved source:') or
            original.startswith('# Directional competition') or original.startswith('# Require directional evidence')):
            backup=self.path.with_name(self.path.stem+'.before_edge_v2.py')
            if not backup.exists():backup.write_text(original,encoding='utf8')
            self.path.write_text(DEFAULT,encoding='utf8')
            self.store.set('policy_kind','edge')
            self.store.event('code','Legacy absolute-confidence policy migrated to relative-edge v2',
                {'backup':backup.name,'old_sha256':hashlib.sha256(original.encode()).hexdigest()})
            self.migrated=True
        self.generation=int(self.store.get('code_generation',0))
        self.last_change=0.;self.kind=self.store.get('policy_kind','edge')
        self.history=[]
        self.attempts=int(self.store.get('evolution_attempts',0))
        self.last_reason=self.store.get('evolution_last_reason','Waiting for 20 labeled samples')
        self._safe_active()
    def _safe_active(self):
        try:self._validate(self.path.read_text(encoding='utf8'))
        except Exception:
            self.path.write_text(DEFAULT,encoding='utf8');self.kind='confidence'
            self.store.event('code','Invalid policy rolled back to built-in template')
    @staticmethod
    def _validate(source):
        tree=ast.parse(source)
        if len(source)>4000:raise ValueError('Oversized policy')
        allowed=(ast.Module,ast.FunctionDef,ast.arguments,ast.arg,ast.Return,ast.If,ast.Assign,
            ast.Name,ast.Load,ast.Store,ast.Constant,ast.Compare,ast.BoolOp,ast.BinOp,
            ast.UnaryOp,ast.Call,ast.Attribute,ast.Subscript,ast.Expr,ast.And,ast.Or,
            ast.Not,ast.NotEq,ast.Eq,ast.Is,ast.IsNot,ast.Lt,ast.LtE,ast.Gt,ast.GtE,ast.Add,ast.Sub,
            ast.Mult,ast.Div,ast.IfExp,ast.keyword)
        for node in ast.walk(tree):
            if not isinstance(node,allowed):raise ValueError('Unsupported policy operation: '+type(node).__name__)
            if isinstance(node,ast.Call):
                if not isinstance(node.func,ast.Name) or node.func.id not in ('max','sorted'):
                    raise ValueError('Policy cannot call external functions')
            if isinstance(node,ast.Attribute) and node.attr not in ('get',):
                raise ValueError('Policy cannot access external attributes')
            if isinstance(node,ast.Name) and node.id.startswith('__'):
                raise ValueError('Policy cannot access internals')
        if not any(isinstance(x,ast.FunctionDef) and x.name=='decide' for x in tree.body):
            raise ValueError('Missing decide function')
        return tree
    def _compile(self,source):
        self._validate(source)
        namespace={'__builtins__':{'max':max,'sorted':sorted}}
        exec(compile(source,'<generated-policy>','exec'),namespace)
        return namespace['decide']
    def adaptive_threshold(self, accuracy=None, samples=0, spread_bps=0.0, *, base=None):
        """Required DIRECTIONAL score margin, not a 60% class confidence.

        The margin adapts slowly with labeled model quality and instrument cost.
        Softmax scores are *not* calibrated expected returns; separately validate
        directional outcomes with delayed labels before allowing LIVE entry.
        """
        params=self._params()
        if 'MIN_EDGE' not in params:
            # Operator-authored legacy policy remains opt-in, never silently edited.
            return float(params.get('CONFIDENCE',.60))
        initial=float(params['MIN_EDGE'] if base is None else base)
        if not math.isfinite(initial):initial=.06
        adjustment=0.
        if accuracy is not None and samples>=100 and math.isfinite(float(accuracy)):
            weight=min(1.,max(0.,(samples-100)/400.))
            adjustment=max(-.025,min(.035,(.53-float(accuracy))*.12))*weight
        cost=max(0.,min(.025,float(spread_bps)*.002)) if math.isfinite(float(spread_bps)) else .025
        return round(min(.25,max(.01,initial+adjustment+cost)),4)

    @staticmethod
    def _safe_scores(scores):
        if not isinstance(scores,dict) or set(scores)!={'BUY','SELL','HOLD'}:return False
        try:
            numbers=[float(scores[k]) for k in ('BUY','SELL','HOLD')]
            return all(math.isfinite(x) and 0<=x<=1 for x in numbers) and abs(sum(numbers)-1)<.025
        except (ValueError,TypeError):return False

    @staticmethod
    def score_edge(scores):
        if not AutoEvolution._safe_scores(scores):return 0.
        action='BUY' if scores['BUY']>=scores['SELL'] else 'SELL'
        other='SELL' if action=='BUY' else 'BUY'
        return float(scores[action])-max(float(scores[other]),float(scores['HOLD']))

    @staticmethod
    def _call_policy(fn,scores,current_direction=None,age=0,threshold=None):
        # The SAME function implements replay and LIVE. For v2 the override is
        # a margin in score points; v1 policies continue to use confidence.
        if threshold is not None:
            if 'MIN_EDGE' in fn.__globals__:
                fn.__globals__['MIN_EDGE']=min(.25,max(.01,float(threshold)))
            elif 'CONFIDENCE' in fn.__globals__:
                fn.__globals__['CONFIDENCE']=min(.85,max(.55,float(threshold)))
        result=fn(scores,current_direction,age)
        if result not in ('BUY','SELL','HOLD','CLOSE'):raise ValueError('Unexpected policy decision')
        return result

    def decision(self,scores,current_direction=None,age=0,threshold=None):
        if not self._safe_scores(scores):return 'HOLD'
        try:
            fn=self._compile(self.path.read_text(encoding='utf8'))
            return self._call_policy(fn,scores,current_direction,age,threshold)
        except Exception as exc:
            self.store.event('code','Policy runtime fault; reverting to previous policy',{'type':type(exc).__name__})
            previous=self.path.with_suffix('.previous.py')
            fallback=previous.read_text(encoding='utf8') if previous.exists() else DEFAULT
            if 'MIN_EDGE' not in fallback:
                fallback=DEFAULT  # do not silently regress to legacy 60% HOLD trap
            try:fn=self._compile(fallback)
            except Exception:
                fallback=DEFAULT;fn=self._compile(fallback)
            self.path.write_text(fallback,encoding='utf8')
            return self._call_policy(fn,scores,current_direction,age,threshold)

    @staticmethod
    def neural_decision(scores,current_direction=None):
        """Raw neural vote on one completed candle (scores only).

        Flat → BUY/SELL/HOLD. In a position this alone must NOT close — live
        exits are broker SL/TP/trail; evaluate_neural_exit only scores intent.
        """
        if not AutoEvolution._safe_scores(scores):return 'HOLD'
        action=max(('BUY','SELL','HOLD'),key=lambda a:float(scores[a]))
        if current_direction is None:return action
        # Legacy callers: still report intent; engine does not market-delete.
        return 'CLOSE' if action in ('BUY','SELL') and action!=current_direction else 'HOLD'

    @staticmethod
    def evaluate_neural_exit(scores,current_direction,*,returns_bps=None,opposite_streak=0,
                             need_streak=None,store=None):
        """Score a CLOSE *intent* from full OHLC zone + multi-bar confirmation.

        Live engine does NOT market-delete on this signal (SL/TP/trail only).
        need_streak comes from self_params (robot-owned), not a frozen default.
        """
        if need_streak is None:
            from .self_params import get as sp_get
            need_streak=int(sp_get(store,'exit_need_streak',2))
        if current_direction not in ('BUY','SELL'):return 'HOLD'
        if not AutoEvolution._safe_scores(scores):return 'HOLD'
        action=max(('BUY','SELL','HOLD'),key=lambda a:float(scores[a]))
        if action not in ('BUY','SELL') or action==current_direction:return 'HOLD'
        if not isinstance(returns_bps,dict):return 'HOLD'
        try:
            r5=float(returns_bps.get('5',0) or 0)
            r15=float(returns_bps.get('15',0) or 0)
        except (TypeError,ValueError):
            return 'HOLD'
        if action=='SELL' and not (r5<0 and r15<0):return 'HOLD'
        if action=='BUY' and not (r5>0 and r15>0):return 'HOLD'
        if int(opposite_streak or 0)<max(1,int(need_streak)):return 'HOLD'
        return 'CLOSE'

    def evaluate_neural_entry(self,scores,*,returns_bps=None,trend=None,regime=None,
                              history=None,readiness=None,spread_bps=0.0):
        """LIVE direction = neural argmax; readiness + economic utility are separate.

        Argmax chooses BUY/SELL/HOLD — zone/SHIFT/trend are evidence only, never
        entry bans. A small BUY/SELL score edge is NOT proven trading advantage:
        when ``readiness`` / ``history`` are supplied, LIVE OPEN also requires
        prep/eval readiness and cost-adjusted economic utility (audit #22–#23).
        Broker news / mandate / TRADEABLE stay in the engine.
        """
        if not AutoEvolution._safe_scores(scores):
            return {'action':'HOLD','proposal':'HOLD','threshold':None,
                    'reason':'Invalid neural scores; no entry','evidence':None,
                    'calibration':None,'score_edge':None,'readiness':readiness,
                    'economic_utility':None}
        buy=float(scores['BUY']);sell=float(scores['SELL']);hold=float(scores['HOLD'])
        direction='BUY' if buy>=sell else 'SELL'
        strength=buy if direction=='BUY' else sell
        score_edge=self.score_edge(scores)
        evidence={'returns_bps':returns_bps,'trend':trend,'regime':regime,
                  'scores':{'BUY':buy,'SELL':sell,'HOLD':hold},
                  'score_edge':score_edge,
                  'score_edge_is_not_trading_advantage':True}
        base={'proposal':direction,'threshold':None,'calibration':None,
              'score_edge':score_edge,'readiness':readiness,'economic_utility':None,
              'evidence':evidence}
        if hold>strength:
            return {**base,'action':'HOLD',
                    'reason':(f'Neural HOLD dominates {direction} '
                              f'({hold:.1%} > {strength:.1%})')}
        # --- Separate gate: model prep/eval readiness (audit #22) ---
        if isinstance(readiness,dict) and readiness.get('ready') is False:
            return {**base,'action':'HOLD',
                    'reason':'Model prep/eval not ready for LIVE OPEN: '+(
                        readiness.get('reason') or 'readiness gate')}
        # --- Separate gate: economic utility after costs (audit #23) ---
        # Argmax keeps the proposal; failure downgrades to HOLD (never flips side).
        # A small score_edge alone is never treated as proven trading advantage.
        economic=None
        labeled=0
        if history is not None:
            labeled=sum(1 for r in history if r.get('label') in (0,1,2))
        if history is not None and labeled>=64:
            edge_floor=max(0.01,min(0.05,float(score_edge) if score_edge is not None else 0.01))
            economic=self.directional_evidence(
                direction,history,edge_floor,forward_holdout=True)
            base['economic_utility']=economic
            evidence['economic_utility']=economic
            if not economic.get('allowed'):
                return {**base,'action':'HOLD',
                        'reason':('Argmax '+direction+' but no cost-adjusted economic utility: '
                                  +str(economic.get('reason') or 'utility gate'))}
        elif isinstance(readiness,dict) and readiness.get('ready'):
            # Prep/eval holdout already measured cost-adjusted proxy — use that,
            # still refusing to treat softmax edge as calibrated profit odds.
            economic={
                'allowed':True,'source':'prep_eval_holdout',
                'samples':readiness.get('eval_samples'),
                'minute_direction_accuracy':readiness.get('minute_direction_accuracy'),
                'holdout_net_bps_mean':readiness.get('holdout_net_bps_mean'),
                'reason':'Prep/eval holdout stands in for economic utility; '
                         'score edge is not calibrated profit probability',
            }
            base['economic_utility']=economic
            evidence['economic_utility']=economic
        elif history is not None:
            return {**base,'action':'HOLD',
                    'reason':('Argmax '+direction+' blocked: need prep/eval readiness or '
                              'cost-adjusted utility evidence (score edge is not enough)')}
        return {**base,'action':direction,
                'reason':(f'Neural argmax {direction} '
                          f'(BUY {buy:.1%} / SELL {sell:.1%} / HOLD {hold:.1%}); '
                          f'score edge {score_edge:.1%} is not calibrated profit probability')}

    @staticmethod
    def _minute_time(value):
        """UTC seconds for a completed-bar timestamp; absent/invalid is NOT safe."""
        if not isinstance(value,str) or not value:
            return None
        try:
            parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
            if parsed.tzinfo is None:
                return None
            return parsed.astimezone(timezone.utc).timestamp()
        except (ValueError,OverflowError):
            return None

    @staticmethod
    def _forward_net_bps(rows, i, action):
        """Spread-adjusted eight-CLOSED-minute *price proxy*, not broker P&L.

        Use only already matured labels, and verify each minute exists. This
        prevents interpolating across market closures or missing data bars.
        Costs via shared ``trade_costs.net_forward_bps`` (one RT spread in bps).
        """
        from .trade_costs import net_forward_bps
        horizon=8
        if i+horizon>=len(rows) or rows[i].get('label') not in (0,1,2):
            return None
        try:
            start=float(rows[i]['price']);end=float(rows[i+horizon]['price'])
            spread0=float(rows[i]['spread_bps']);spread8=float(rows[i+horizon]['spread_bps'])
            if (not all(map(math.isfinite,(start,end,spread0,spread8))) or
                start<=0 or end<=0 or spread0<0 or spread8<0):
                return None
            previous=AutoEvolution._minute_time(rows[i].get('time'))
            if previous is None:return None
            for j in range(i+1,i+horizon+1):
                current=AutoEvolution._minute_time(rows[j].get('time'))
                if current is None or abs(current-previous-60.)>.5:
                    return None
                previous=current
            return net_forward_bps(action,start,end,
                                   spread_bps_entry=spread0,spread_bps_exit=spread8)
        except (KeyError,TypeError,ValueError,ZeroDivisionError):
            return None

    @staticmethod
    def _edge_summary(values):
        if not values:return None
        # Robust cap: one extreme news candle must not justify repeated entries.
        magnitudes=sorted(abs(x) for x in values)
        median=magnitudes[len(magnitudes)//2]
        cap=max(0.001,3.*median)
        bounded=[max(-cap,min(cap,v)) for v in values]
        mean=sum(bounded)/len(bounded)
        variance=sum((v-mean)**2 for v in bounded)/len(bounded)
        lower=mean-1.282*math.sqrt(variance/max(1,len(bounded)))
        return {'mean_net_bps':round(mean,4),'lower_net_bps':round(lower,4),
                'samples':len(bounded),'positive':lower>0.}

    def directional_evidence(self,action,history,threshold, *, min_examples=8,
                             warmup=True,forward_holdout=False):
        """Check a *realized* spread-adjusted price-move proxy, not 60% wins.

        Forecasts are selected from completed bars only. The next 8 minutes
        must be present and chronological; observations cannot overlap. If
        forward_holdout=True, a later block separately must support the edge.
        Neither proxy includes financing, slippage or commissions, so it is
        not guaranteed net trading P&L or an estimate of calibrated odds.
        """
        if action not in ('BUY','SELL'):
            return {'allowed':False,'reason':'No directional proposal','samples':0}
        rows=list(history)[-900:]
        resolved=sum(1 for r in rows if r.get('label') in (0,1,2) and
                     self._safe_scores(r.get('scores')))
        if warmup and resolved<64:
            return {'allowed':False,'reason':f'Waiting for delayed labels: {resolved}/64','samples':0}
        fn=self._compile(self.path.read_text(encoding='utf8'))
        matched=[];last_selected=-8
        missing_prices=0
        for i,record in enumerate(rows):
            if i-last_selected<8 or record.get('label') not in (0,1,2) or not self._safe_scores(record.get('scores')):
                continue
            if self._call_policy(fn,record['scores'],threshold=threshold)!=action:
                continue
            net=self._forward_net_bps(rows,i,action)
            if net is None:
                missing_prices+=1
                continue
            matched.append((record,net))
            last_selected=i
        total=len(matched)
        # The independent observations are at least one full prediction
        # horizon apart, so all outcome data preceded the current LIVE signal.
        if total<min_examples:
            return {'allowed':False,
                    'reason':f'Need {min_examples} valid independent {action} price outcomes: {total} available'
                             + (' (missing completed contiguous price bars)' if missing_prices else ''),
                    'samples':total}
        expected=0 if action=='BUY' else 1
        precision=sum(int(r['label']==expected) for r,_ in matched)/total
        summary=self._edge_summary([x for _,x in matched]);assert summary is not None
        evidence={'allowed':False,'samples':total,'precision':round(precision,4),
                  'metric':'eight_minute_spread_adjusted_price_proxy',
                  'mean_net_bps':summary['mean_net_bps'],'lower_net_bps':summary['lower_net_bps']}
        if forward_holdout:
            # Training observations cannot contaminate later validation moves.
            cut=int(total*.70)
            training=[x for _,x in matched[:max(0,cut-1)]]
            validation=[x for _,x in matched[cut:]]
            evidence['train_samples']=len(training)
            evidence['validation_samples']=len(validation)
            if len(training)<8 or len(validation)<4:
                evidence['reason']='Need 8 earlier and 4 later independent price outcomes for forward validation'
                return evidence
            train=self._edge_summary(training);test=self._edge_summary(validation)
            evidence['train_lower_net_bps']=train['lower_net_bps']
            evidence['validation_lower_net_bps']=test['lower_net_bps']
            if not (train['positive'] and test['positive']):
                evidence['reason']='Price edge not confirmed on separate training and forward periods'
                return evidence
        elif not summary['positive']:
            evidence['reason']='Spread-adjusted price edge not positive with uncertainty allowance'
            return evidence
        evidence['allowed']=True
        evidence['reason']='Positive spread-adjusted price edge verified on chronological observations'
        return evidence

    def calibrate_entry_margin(self,action,history,baseline):
        """Find a less restrictive score margin ONLY if walk-forward labels support it.

        Train selects the margin without peeking at the validation block. A
        disjoint later period must then independently pass. No LIVE orders are
        placed in this calibration. Proxy outcomes are NOT calibrated P&L.
        """
        result={'baseline':baseline,'selected':baseline,'method':'policy_default',
                'reason':'No lower margin verified on chronological holdout'}
        if action not in ('BUY','SELL') or 'MIN_EDGE' not in self._params():
            return result
        resolved=[r for r in history if r.get('label') in (0,1,2) and
                  self._safe_scores(r.get('scores'))]
        resolved=resolved[-800:]
        if len(resolved)<96:
            result['reason']=f'Need 96 resolved labels for margin calibration ({len(resolved)} available)'
            return result
        # Eight-completed-candle labels overlap: embargo a full horizon at the
        # training/validation boundary to prevent reusing the same price move.
        cut=int(len(resolved)*.70)
        train=resolved[:max(0,cut-8)]
        validation=resolved[cut:]
        # Fixed, interpretable search grid. The lowest allowed margin still
        # requires the current direction to beat BOTH alternatives by 1 pp.
        candidates=sorted({x for x in (.01,.015,.02,.025,.03,.04,.05,round(baseline,4))
                           if .01<=x<baseline-1e-8})
        chosen=None;train_result=None
        for candidate in candidates:
            check=self.directional_evidence(action,train,candidate,
                                            min_examples=8,warmup=False)
            if check['allowed']:
                chosen=candidate;train_result=check
                break
        if chosen is None:
            result['reason']='No lower margin passed independent training examples'
            return result
        heldout=self.directional_evidence(action,validation,chosen,
                                          min_examples=4,warmup=False)
        result['train_samples']=train_result['samples']
        result['train_precision']=train_result['precision']
        result['validation_samples']=heldout['samples']
        result['validation_precision']=heldout.get('precision')
        if not heldout['allowed']:
            result['reason']='Lower margin failed chronological holdout: '+heldout['reason']
            return result
        result.update(selected=chosen,method='holdout_calibrated',
                      reason='Lower margin passed separate training and forward holdout checks')
        return result

    def evaluate_entry(self,scores,history,accuracy=None,samples=0,spread_bps=0.0):
        """Single LIVE decision with optional out-of-sample margin calibration.

        A raw winner is NOT an order. A lower margin is allowed only when
        independent delayed labels confirm its directional quality and a
        chronological holdout verifies it. Other broker gates are unchanged.
        """
        baseline=self.adaptive_threshold(accuracy,samples,spread_bps)
        calibration={'baseline':baseline,'selected':baseline,'method':'policy_default'}
        if not self._safe_scores(scores):
            return {'action':'HOLD','proposal':'HOLD','threshold':baseline,
                    'reason':'Invalid neural scores','evidence':None,'calibration':calibration}
        action='BUY' if scores['BUY']>=scores['SELL'] else 'SELL'
        if scores['HOLD']>=scores[action]:
            return {'action':'HOLD','proposal':'HOLD','threshold':baseline,
                    'reason':'Neural HOLD ranks first','evidence':None,'calibration':calibration}
        # Fit the same score-margin unit used by the evolved policy, never a
        # percentage/price/return mix-up. This is NOT an unconditional bypass.
        calibration=self.calibrate_entry_margin(action,history,baseline)
        threshold=calibration['selected']
        proposal=self.decision(scores,threshold=threshold)
        if proposal not in ('BUY','SELL'):
            return {'action':'HOLD','proposal':proposal,'threshold':threshold,
                    'reason':f'Directional advantage {self.score_edge(scores):.1%} below required {threshold:.1%}; '
                             +calibration.get('reason','policy margin'),
                    'evidence':None,'calibration':calibration}
        evidence=self.directional_evidence(proposal,history,threshold,forward_holdout=True)
        return {'action':proposal if evidence['allowed'] else 'HOLD','proposal':proposal,
                'threshold':threshold,'reason':evidence['reason'],'evidence':evidence,
                'calibration':calibration}

    def _test_candidate(self,source):
        self._validate(source)
        with tempfile.TemporaryDirectory() as d:
            policy=Path(d)/'generated.py';policy.write_text(source,encoding='utf8')
            checker=Path(d)/'checker.py'
            checker.write_text('''import runpy,sys\np=runpy.run_path(sys.argv[1]);f=p['decide']\nassert f({'BUY':.1,'SELL':.1,'HOLD':.8})=='HOLD'\nassert f({'BUY':.99,'SELL':.005,'HOLD':.005})=='BUY'\nassert f({'BUY':.005,'SELL':.99,'HOLD':.005},'BUY',10)=='CLOSE'\nassert f({'BUY':.005,'SELL':.99,'HOLD':.005},'BUY',0)=='HOLD'\nfor s in [{'BUY':.7,'SELL':.1,'HOLD':.2},{'BUY':.1,'SELL':.7,'HOLD':.2}]:\n assert f(s) in ('BUY','SELL','HOLD')\n''',encoding='utf8')
            try:
                # In a PyInstaller executable sys.executable points to the bundled
                # NeuralTradingAI.exe, not a Python interpreter. Start its dedicated
                # policy-check worker instead of passing it a .py script.
                if getattr(sys, 'frozen', False):
                    cmd=[sys.executable,'--policy-check',str(policy)]
                else:
                    cmd=[sys.executable,'-I',str(checker),str(policy)]
                result=subprocess.run(cmd, capture_output=True,timeout=20,check=False)
                return result.returncode==0
            except (OSError,subprocess.TimeoutExpired):return False
    @staticmethod
    def _replay(f,history,margin_override=None):
        """Class-balanced delayed-label replay, with an explicit abstention cost.

        Each observed class contributes equally, so an always-HOLD strategy
        cannot win merely because the dataset contains many HOLD labels.
        This is a classification proxy, not a claim of trading profitability.
        """
        groups={0:[],1:[],2:[]}
        for item in history:
            label=item.get('label')
            if label not in groups:continue
            try:
                if margin_override is None:signal=f(item['scores'])
                else:signal=AutoEvolution._call_policy(f,item['scores'],threshold=margin_override)
            except Exception:continue
            expected=('BUY','SELL','HOLD')[label]
            # Replay penalty in price-relative basis points, not nominal ticks;
            # one tick has very different economic meaning across instruments.
            spread=min(.2,abs(item.get('spread_bps',0))*.01)
            if signal==expected:
                reward=(1.0-spread) if signal!='HOLD' else .35
            elif signal=='HOLD':
                reward=-.25  # missed directional opportunity
            else:
                reward=-1.0-spread if label!=2 else -.6-spread
            groups[label].append(reward)
        present=[sum(values)/len(values) for values in groups.values() if values]
        n=sum(map(len,groups.values()))
        # Require BUY and SELL evidence before a strategy can be promoted.
        if not groups[0] or not groups[1]:return -1.0,0
        return sum(present)/len(present),n
    def _params(self):
        src=self.path.read_text(encoding='utf8')
        tree=self._validate(src)
        constants={}
        for a in tree.body:
            if isinstance(a,ast.Assign) and len(a.targets)==1 and isinstance(a.targets[0],ast.Name) and isinstance(a.value,ast.Constant):
                constants[a.targets[0].id]=a.value.value
        return constants
    def evolve(self,accuracy,sample_count,history=None,force=False,loss_trade=False):
        """Search/promote policy candidates.

        Normal path: every 10 new labels (background).
        force=True: run immediately after a closed trade so evolution keeps up
        with live trading tempo; loss_trade lowers the sample floor.
        """
        min_samples=4 if (force and loss_trade) else (6 if force else 10)
        if sample_count<min_samples:
            self.last_reason=f'Waiting for labels: {sample_count}/{min_samples}'
            return None
        if not force:
            if sample_count%10!=0:return None
            # One attempt per NEW labeled milestone, not another attempt every poll.
            if sample_count<=self.store.get('evo_last_labeled_v3',-1):return None
            if time.monotonic()-self.last_change<4:return None
        else:
            # Trade-timed: short debounce only — do not wait for a stale milestone.
            if time.monotonic()-self.last_change<(0.5 if loss_trade else 1.0):return None
        self.store.set('evo_last_labeled_v3',max(sample_count,int(self.store.get('evo_last_labeled_v3',-1) or -1)))
        self.attempts+=1
        self.store.set('evolution_attempts',self.attempts)
        self.last_reason='Evaluating candidate strategies'
        self.store.event('code','Evolution evaluation started',{'samples':sample_count,'attempt':self.attempts})
        self.last_change=time.monotonic()
        values=self._params()
        edge=float(values.get('MIN_EDGE',.06))
        variations=sorted({.01,.015,.02,.025,.04,.06,.08,.10,.12,.16,
            round(min(.25,max(.01,edge-.02)),3),round(min(.25,max(.01,edge+.02)),3)})
        variants=[]
        for kind in EDGE_POLICY_KINDS:
            for margin in variations:
                source=TEMPLATES[kind].format(confidence=.60,reversal=max(.30,round(margin+.20,2)),
                    min_hold=3,margin=margin)
                if source!=self.path.read_text(encoding='utf8'):variants.append((kind,margin,source))
        old_source=self.path.read_text(encoding='utf8')
        incumbent=self._compile(old_source)
        # In replay, dynamically resolve the same margin scale as in LIVE.
        # Use the labeled-history mean spread as a per-instrument cost proxy.
        history=history or []
        spreads=[float(x.get('spread_bps',0)) for x in history if x.get('label') in (0,1,2)
                 and math.isfinite(float(x.get('spread_bps',0)))]
        mean_spread=sum(spreads)/len(spreads) if spreads else 0.
        def replay_with_threshold(fn,raw_base):
            margin=self.adaptive_threshold(accuracy,sample_count,mean_spread,base=raw_base)
            return self._replay(fn,history,margin_override=margin)
        baseline,n=replay_with_threshold(incumbent,edge)
        # Chronological holdout; don't promote a strategy that only fits
        # older examples. Labels were delayed and instrument-scoped upstream.
        eligible=[row for row in history if row.get('label') in (0,1,2)]
        cutoff=int(len(eligible)*.7)
        train=eligible[:cutoff]
        validation=eligible[cutoff:]
        old_validation,_=self._replay(incumbent,validation,
            margin_override=self.adaptive_threshold(accuracy,sample_count,mean_spread,base=edge))
        old_train,_=self._replay(incumbent,train,
            margin_override=self.adaptive_threshold(accuracy,sample_count,mean_spread,base=edge))
        winner=None;best=baseline
        # Promote on a trade/learning tempo. Generation gates EXIT policy code;
        # LIVE entry is neural vote (lot+market = risk frame).
        need_n=12 if force else 18
        need_val=4 if force else 6
        lift=.001 if (force and loss_trade) else .002
        for kind,val,source in variants:
            try:
                fn=self._compile(source)
                reward,_=replay_with_threshold(fn,val)
                new_margin=self.adaptive_threshold(accuracy,sample_count,mean_spread,base=val)
                train_score,_=self._replay(fn,train,margin_override=new_margin)
                validation_score,validation_n=self._replay(fn,validation,margin_override=new_margin)
                if (n>=need_n and validation_n>=need_val and train_score>=old_train and
                    validation_score>old_validation+lift and reward>best+lift):
                    best=reward;winner=(kind,val,source)
            except Exception:continue
        if not winner:
            self.last_reason=(f'Waiting for BUY/SELL replay (minimum {need_n} labels)' if n<need_n
                              else 'No relative-edge candidate passed training and forward holdout replay')
            self.store.set('evolution_last_reason',self.last_reason)
            self.store.event('code','Evolution candidate not promoted',{'reason':self.last_reason,'samples':n})
            return None
        kind,val,source=winner
        if not self._test_candidate(source):
            self.last_reason='Candidate failed isolated tests'
            self.store.set('evolution_last_reason',self.last_reason)
            self.store.event('code','Generated policy failed isolated checks; not activated',{'kind':kind})
            return None
        previous=self.path.with_suffix('.previous.py');nextfile=self.path.with_suffix('.next.py')
        previous.write_text(old_source,encoding='utf8');nextfile.write_text(source,encoding='utf8')
        os.replace(nextfile,self.path)
        try:
            self._compile(self.path.read_text(encoding='utf8'))
        except Exception:
            os.replace(previous,self.path);raise
        self.generation+=1;self.kind=kind
        self.last_reason='Candidate promoted after isolated tests'
        self.store.set('evolution_last_reason',self.last_reason)
        self.store.set('code_generation',self.generation);self.store.set('policy_kind',kind)
        # Do NOT bump learning_generation — policy file ≠ LIVE neural progress.
        digest=hashlib.sha256(source.encode()).hexdigest()
        self.store.event('code','Python policy automatically generated, tested and activated',
            {'generation':self.generation,'type':kind,'required_edge':val,'hash':digest,
             'replay_score_old':baseline,'replay_score_new':best,'samples':n})
        return val
    def info(self):
        source=self.path.read_text(encoding='utf8')
        return {'generation':self.generation,'strategy':self.kind,'source':source,
                'sha256':hashlib.sha256(source.encode()).hexdigest(),
                'attempts':self.attempts,'last_reason':self.last_reason}

    CHECKPOINT_TRADES_KEY='model_checkpoint_at_trades'
    ROLLBACK_COOLDOWN_KEY='model_rollback_cooldown_until_trades'
    LAST_PROMOTE_KEY='shadow_last_promote_v1'

    @staticmethod
    def weight_checksum(core):
        """Stable fingerprint of neural weights — blocks identical re-promote."""
        if core is None:return None
        h=hashlib.sha256()
        for name in ('P','W','R','O','b','G','tau','tau_c'):
            arr=getattr(core,name,None)
            if arr is None:continue
            h.update(np.ascontiguousarray(arr,dtype=np.float64).tobytes())
        return h.hexdigest()[:40]

    # --- Audit #18: controlled weight-update safety helpers -------------------

    @staticmethod
    def snapshot_core_weights(core):
        """Prior-state snapshot before a batch/online weight update."""
        if core is None:return None
        if hasattr(core,'snapshot_weights'):
            return core.snapshot_weights()
        from .core import WEIGHT_PARAM_NAMES
        return {n:np.asarray(getattr(core,n),dtype=float).copy()
                for n in WEIGHT_PARAM_NAMES if hasattr(core,n)}

    @staticmethod
    def restore_core_weights(core, snap):
        """Restore prior weights when post-update criterion worsens."""
        if core is None or snap is None:return False
        if hasattr(core,'restore_weights'):
            return bool(core.restore_weights(snap))
        from .core import WEIGHT_PARAM_NAMES
        for n in WEIGHT_PARAM_NAMES:
            if n not in snap:return False
            setattr(core,n,np.asarray(snap[n],dtype=float).copy())
        return True

    @staticmethod
    def weight_update_delta_norm(before, after):
        from .core import NeuralCore
        return NeuralCore.weight_delta_norm(before,after)

    @staticmethod
    def controlled_weight_learn(core, transition, label, *, lr=None,
                                objective=None, max_delta_norm=None,
                                restore_if_worsens=True, criterion_fn=None):
        """Snapshot → bounded learn → optional criterion restore (audit #18).

        Ordinary learn is never reported as proven_improvement. If
        `criterion_fn(core) -> float` is supplied (higher=better) and the
        post-update value is worse than pre-update, weights restore.
        """
        if core is None or not hasattr(core,'learn'):
            return {'applied':False,'proven_improvement':False,'reason':'no_core'}
        prior=AutoEvolution.snapshot_core_weights(core)
        pre_crit=None
        if criterion_fn is not None:
            try:pre_crit=float(criterion_fn(core))
            except Exception:pre_crit=None
        loss=core.learn(transition,label,lr=lr,objective=objective,
                        max_delta_norm=max_delta_norm,
                        restore_if_worsens=restore_if_worsens)
        meta=dict(getattr(core,'last_learn_meta',None) or {})
        restored=bool(meta.get('restored'))
        if (criterion_fn is not None and pre_crit is not None and not restored
                and prior is not None):
            try:post_crit=float(criterion_fn(core))
            except Exception:post_crit=None
            if post_crit is not None and post_crit<pre_crit:
                AutoEvolution.restore_core_weights(core,prior)
                restored=True
                meta['restored']=True
                meta['restore_reason']='external_criterion_worsened'
                meta['criterion_before']=pre_crit
                meta['criterion_after']=post_crit
                if hasattr(core,'last_learn_meta'):
                    core.last_learn_meta=meta
        meta['proven_improvement']=False
        if hasattr(core,'last_learn_meta'):
            core.last_learn_meta=meta
        after=AutoEvolution.snapshot_core_weights(core)
        return {
            'applied':loss is not None and not restored,
            'loss':loss,
            'restored':restored,
            'clipped':bool(meta.get('clipped')),
            'delta_norm':meta.get('delta_norm'),
            'objective':meta.get('objective'),
            'objective_weight':meta.get('objective_weight'),
            'proven_improvement':False,
            'meta':meta,
            'weights_changed':(
                prior is not None and after is not None and
                AutoEvolution.weight_update_delta_norm(prior,after)>1e-15),
        }

    def _promote_evidence_boundary(self, live_hist, shadow_rows):
        """Trade counts used as the evaluation frontier for anti-repeat promote."""
        return {
            'live_trades':len(live_hist or []),
            'shadow_trades':len(shadow_rows or []),
        }

    def _promote_blocked_same_evidence(self, engine, live_hist, shadow_rows, checksum):
        """True when checksum + trade boundary match the last promote.

        model_id is stored for audit. After a successful promote the shadow
        version may advance; blocking still hinges on identical weights and
        no new evaluation trades — not on the bumped id alone.
        """
        del engine  # reserved for future model_id cross-checks
        prev=self.store.get(self.LAST_PROMOTE_KEY) or {}
        if not isinstance(prev,dict) or not prev:return False
        boundary=self._promote_evidence_boundary(live_hist,shadow_rows)
        same_checksum=(prev.get('weight_checksum') is not None and
                       checksum is not None and
                       str(prev.get('weight_checksum'))==str(checksum))
        same_boundary=(int(prev.get('live_trades') or -1)==boundary['live_trades'] and
                       int(prev.get('shadow_trades') or -1)==boundary['shadow_trades'])
        return bool(same_checksum and same_boundary)

    def _record_promote_evidence(self, engine, live_hist, shadow_rows, checksum):
        boundary=self._promote_evidence_boundary(live_hist,shadow_rows)
        shadow=getattr(engine,'shadow_brain',None)
        meta={
            'model_id':int(getattr(shadow,'model_version',0) or 0),
            'weight_checksum':checksum,
            'live_trades':boundary['live_trades'],
            'shadow_trades':boundary['shadow_trades'],
            'at':datetime.now(timezone.utc).isoformat(),
        }
        self.store.set(self.LAST_PROMOTE_KEY,meta)
        return meta

    def _push_model_snapshot(self, engine, *, note='checkpoint'):
        """Save neural weights + registry specs onto the model stack."""
        mv=getattr(engine,'model_validator',None)
        brain=getattr(engine,'brain',None)
        if mv is None or brain is None:return None
        path=None
        if hasattr(brain,'backup_weights'):
            try:path=brain.backup_weights(tag=str(note)[:24] or 'ckpt')
            except Exception:path=None
        specs=[]
        reg=getattr(engine,'feature_registry',None)
        if reg is not None:
            specs=[dict(x) for x in (reg.approved or [])]
        from .instrument_isolation import label_backup_meta
        epic=getattr(brain,'epic',None)
        if not epic and getattr(engine,'mandate',None):
            epic=engine.mandate.get('epic')
        meta=label_backup_meta({
            'model_version':int(getattr(brain,'model_version',0) or 0),
            'feature_schema':getattr(brain,'feature_schema',None),
            'ohlc_dim':getattr(brain,'ohlc_dim',None),
            'fusion_dim':getattr(brain,'fusion_dim',None),
            'approved':list(reg.approved_ids()) if reg is not None else [],
            'approved_specs':specs,
            'weights_path':path,
            'note':str(note)[:80],
        },epic)
        mv.push_version(meta)
        return meta

    def _ensure_model_baseline(self, engine):
        """Push one weight+registry snapshot if the stack is empty."""
        mv=getattr(engine,'model_validator',None)
        if mv is None:return None
        if mv.stack:return mv.stack[-1]
        return self._push_model_snapshot(engine,note='baseline seed')

    def _maybe_checkpoint_weights(self, engine, n_trades, win):
        """Every `win` broker trades, snapshot weights so rollback has a prior."""
        win=max(1,int(win))
        last=int(self.store.get(self.CHECKPOINT_TRADES_KEY) or 0)
        if int(n_trades)-last<win:
            return None
        self._ensure_model_baseline(engine)
        meta=self._push_model_snapshot(engine,note=f'checkpoint@{n_trades}')
        self.store.set(self.CHECKPOINT_TRADES_KEY,int(n_trades))
        return meta

    def shadow_self_dev_tick(self, engine):
        """Side-by-side shadow NeuralCore vs live using *cash-comparable* paper £.

        Shadow runs its own paper journal. Price moves are scaled to account
        currency via LIVE broker_£/price ratios — never raw price units vs £.
        Compare on the same time window / single model_version per side.
        """
        from .model_validation import compare_models, aligned_window_pnls
        from .self_params import (load as load_params, evolve_from_feedback,
                                  maybe_commit_long_horizon)
        engine._ensure_shadow_brain()
        # Seed baseline, then checkpoint every rollback-window of broker trades.
        self._ensure_model_baseline(engine)
        cfg=load_params(self.store)
        min_trades=int(cfg['shadow_min_broker_trades'])
        win=int(cfg['shadow_rollback_window'])
        delta=float(cfg['shadow_rollback_delta'])
        oos_frac=float(cfg['val_oos_frac'])
        live_hist=[r for r in (getattr(engine.profit_learning,'history',[]) or [])
                   if r.get('actual_pnl') is not None]
        self._maybe_checkpoint_weights(engine,len(live_hist),win)
        paper=getattr(engine,'shadow_paper',None)
        if paper is not None:
            paper.update_cash_scale(live_hist)
        shadow_rows=(paper.paper_rows() if paper and hasattr(paper,'paper_rows')
                     else [])
        if not shadow_rows and paper is not None:
            shadow_rows=[r for r in (paper.history or [])
                         if r.get('unit')=='account_currency'
                         and r.get('paper_pnl') is not None]
        if len(live_hist)<min_trades or len(shadow_rows)<max(4,min_trades//2):
            self.last_reason=('Shadow self-dev waiting for broker £ + cash-scaled '
                              'paper trades')
            return None
        aligned=aligned_window_pnls(
            live_hist,shadow_rows,oos_frac=oos_frac,
            inc_cash_key='actual_pnl',cand_cash_key='paper_pnl')
        live_pnls=aligned['incumbent_pnls']
        shadow_pnls=aligned['candidate_pnls']
        decision=compare_models(
            live_pnls,shadow_pnls,store=self.store,
            incumbent_version=aligned.get('incumbent_version'),
            candidate_version=aligned.get('candidate_version'))
        decision=dict(decision,
                      incumbent_version=aligned.get('incumbent_version'),
                      candidate_version=aligned.get('candidate_version'),
                      window_start=aligned.get('window_start'),
                      window_end=aligned.get('window_end'))
        evolve_from_feedback(self.store,shadow_promote=bool(decision.get('promote')),
                             commit=False)
        maybe_commit_long_horizon(self.store,engine.profit_learning.broker_pnls())
        # Real promote: shadow paper £ beat LIVE → copy shadow weights into LIVE.
        if decision.get('promote') and getattr(engine,'shadow_brain',None) is not None:
            brain=getattr(engine,'brain',None)
            shadow=engine.shadow_brain
            checksum=self.weight_checksum(shadow)
            if self._promote_blocked_same_evidence(engine,live_hist,shadow_rows,checksum):
                self.last_reason=('Shadow promote blocked — same model/checksum/'
                                  'trade boundary as last promote')
                decision=dict(decision,promote=False,weights_promoted=False,
                              reason=self.last_reason)
            elif (brain is not None and shadow is not None and
                    getattr(brain,'core',None) is not None and shadow.d==brain.core.d):
                self._push_model_snapshot(engine,note='pre_shadow_promote')
                # Full architecture adopt — never silently drop shadow neurons
                # when shadow.n != live.n (partial min(n) copy was a fake promote).
                if brain.core.copy_weights_from(shadow,adopt_architecture=True):
                    brain.model_version=int(getattr(brain,'model_version',0) or 0)+1
                    try:brain.save()
                    except Exception:pass
                    from .learning_generation import bump as bump_generation
                    bumped=bump_generation(
                        self.store,'shadow_weights_promoted_to_live',
                        kind='verified_candidate_promotes',
                        weight_checksum=checksum)
                    self._record_promote_evidence(
                        engine,live_hist,shadow_rows,checksum)
                    shadow.model_version=int(getattr(shadow,'model_version',0) or 0)+1
                    if hasattr(engine,'_persist_shadow_state'):
                        engine._persist_shadow_state()
                    self.last_reason='Shadow weights promoted to LIVE neural core'
                    self.store.event('learning','Shadow neural weights promoted to LIVE',
                        {'model_version':brain.model_version,
                         'shadow_neurons':shadow.n,'live_neurons':brain.core.n,
                         'weight_checksum':checksum,'learning_generation':bumped,
                         'architecture_adopted':True})
                    decision=dict(decision,weights_promoted=True,
                                  weight_checksum=checksum,
                                  architecture_adopted=True)
        if len(live_hist)>=win*2:
            recent=sum(r['actual_pnl'] for r in live_hist[-win:])
            prior=sum(r['actual_pnl'] for r in live_hist[-2*win:-win])
            cool_until=int(self.store.get(self.ROLLBACK_COOLDOWN_KEY) or 0)
            if recent<prior-delta and len(live_hist)>=cool_until:
                target=engine.model_validator.rollback_target()
                if target is None:
                    # First bad window with empty stack — snapshot now as baseline
                    # for the *next* window; cannot invent an older brain.
                    self._push_model_snapshot(engine,note='late_baseline_after_drawdown')
                    self.store.set(self.CHECKPOINT_TRADES_KEY,len(live_hist))
                    self.last_reason=('Shadow wants rollback (recent £ worse); '
                                      'saved baseline — will restore on next drawdown')
                    return {'rollback':False,'reason':'seeded_baseline','decision':decision}
                restore=getattr(engine,'restore_neural_weights',None)
                prev=engine.model_validator.mark_rollback(
                    'recent broker £ worse than prior window',
                    restore_fn=restore,target=target)
                restored=bool(prev and prev.get('weights_restored'))
                ver=prev.get('model_version') if prev else None
                if restored:
                    self.last_reason=f'Shadow rollback restored weights+registry v{ver}'
                    # Don't thrash — wait another window before re-rolling.
                    self.store.set(self.ROLLBACK_COOLDOWN_KEY,len(live_hist)+win)
                    self.store.set(self.CHECKPOINT_TRADES_KEY,len(live_hist))
                elif ver is not None:
                    self.last_reason=f'Shadow rollback failed for v{ver}'
                else:
                    self.last_reason='Shadow rollback failed (snapshot incomplete)'
                return {'rollback':True,'previous':prev,'decision':decision,
                        'weights_restored':restored,
                        'registry_restored':bool(prev and prev.get('registry_restored'))}
        self.store.set('shadow_self_dev_last',{
            'decision':decision,
            'shadow_neurons':getattr(engine.shadow_brain,'n',None),
            'shadow_dim':getattr(engine.shadow_brain,'d',None),
            'live_samples':len(live_hist),'paper_cash_samples':len(shadow_rows),
            'method':'independent_shadow_paper_cash',
            'aligned_window':{
                'start':aligned.get('window_start'),'end':aligned.get('window_end'),
                'incumbent_version':aligned.get('incumbent_version'),
                'candidate_version':aligned.get('candidate_version')},
            'paper_summary':paper.summary() if paper else None,
            'params':{'min_trades':min_trades,'window':win,'delta':delta}})
        if decision.get('weights_promoted'):
            pass
        elif decision.get('promote'):
            self.last_reason='Shadow paper cash beats live OOS broker £'
        else:
            self.last_reason=(decision.get('reason')
                              or 'Shadow paper cash not better than live OOS broker £')
        return decision

    def promote_shadow_policy(self, candidate_source, *, force=False):
        """Promote a validated policy source with .previous.py rollback file."""
        if not force and not self._test_candidate(candidate_source):
            self.last_reason='Shadow policy failed isolated tests'
            return False
        old=self.path.read_text(encoding='utf8')
        previous=self.path.with_suffix('.previous.py')
        nextfile=self.path.with_suffix('.next.py')
        previous.write_text(old,encoding='utf8')
        nextfile.write_text(candidate_source,encoding='utf8')
        os.replace(nextfile,self.path)
        try:
            self._compile(self.path.read_text(encoding='utf8'))
        except Exception:
            os.replace(previous,self.path)
            self.last_reason='Shadow promote compile failed; restored previous'
            return False
        self.generation+=1
        self.store.set('code_generation',self.generation)
        # Policy file only — leave learning_generation for neural/feature/£ proof.
        self.last_reason='Shadow policy promoted with rollback file'
        self.store.event('code','Shadow-validated policy activated',
            {'generation':self.generation})
        return True

    def rollback_policy(self):
        """Restore active_policy.py from .previous.py if present."""
        previous=self.path.with_suffix('.previous.py')
        if not previous.exists():
            self.last_reason='No previous policy to restore'
            return False
        os.replace(previous,self.path)
        self._compile(self.path.read_text(encoding='utf8'))
        self.last_reason='Policy rolled back to previous version'
        self.store.event('code','Policy rolled back to previous version',{})
        return True
