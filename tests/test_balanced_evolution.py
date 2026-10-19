from app.autonomy import AutoEvolution, TEMPLATES


def test_balanced_replay_penalizes_always_hold():
    history=[]
    for label in (0,1,2):
        for _ in range(30):
            scores=[{'BUY':.9,'SELL':.05,'HOLD':.05}, {'BUY':.05,'SELL':.9,'HOLD':.05}, {'BUY':.05,'SELL':.05,'HOLD':.9}][label]
            history.append({'label':label,'scores':scores,'spread_ticks':1})
    always_hold=lambda scores: 'HOLD'
    directional=lambda scores: max(scores,key=scores.get)
    hold_score,n=AutoEvolution._replay(always_hold,history)
    directional_score,n2=AutoEvolution._replay(directional,history)
    assert n==n2==90
    assert directional_score>hold_score
    assert len(TEMPLATES)>=5


def test_replay_needs_both_directions():
    score,n=AutoEvolution._replay(lambda scores:'HOLD',[{'label':2,'scores':{'BUY':.1,'SELL':.1,'HOLD':.8}}]*80)
    assert n==0
