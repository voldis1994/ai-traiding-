"""Single coherent contract for the self-learning trading machine.

This module does not execute trades. It documents the LIVE decision path so
version history (v3 features / v4 fusion / v6 argmax) is not confused with
competing LIVE vetoes. Full Latvian explanation: PASMACISANAS_MASINA_LV.md
"""
from .core import NeuralCore
from .market_context import FEATURE_DIM, FEATURE_SCHEMA
from .market_fusion import FUSION_DIM, FUSION_SCHEMA

# Pipeline: F_learn ∘ F_broker ∘ F_order ∘ F_decision ∘ F_neuron
LIVE_DECISION_RULE = 'neural_argmax_buy_sell_hold'
LIVE_DECISION_BAR = 'completed_broker_M1'
POLL_SECONDS = 3
NEURAL_DT_SECONDS = 60
DEFAULT_LEARNING_RATE = NeuralCore.DEFAULT_LR  # 0.1 — visible ~0.01 weight steps
LABEL_HORIZON_BARS = 1  # next M1 bar — same tempo as trading decisions
# Closed-trade P&L regret is the primary corrective teacher (engine._learn_closed_trade).
PRIMARY_FEEDBACK = 'closed_trade_pnl_regret_or_chop_hold'
FEATURE_SCHEMAS = {
    'ohlc_v3': {'schema': FEATURE_SCHEMA, 'dim': FEATURE_DIM},
    'fusion_v4': {'schema': FUSION_SCHEMA, 'dim': FUSION_DIM},
    'dynamic': {'note': 'FeatureRegistry may append validated OHLC formulas; dims grow at runtime'},
}
# Present in autonomy templates / replay only — NOT a LIVE entry veto.
SHADOW_ONLY = ('evolved_MIN_EDGE', 'legacy_60pct_gates', 'price_edge_v5_entry_filter',
               'shadow_architecture_probe', 'feature_discovery_proposals')
SELF_DEV_CYCLE = (
    'collect_ohlc',
    'discover_candidates',
    'walk_forward_validate_features',
    'expand_neural_inputs',
    'profit_learning_labels',
    'shadow_compare_models',
    'promote_or_rollback',
)
# Capital.com does not provide true volume / DOM — discovery never invents them.
DISCOVERY_DATA = ('ohlc','spread','peers','scheduled_news')
# Tunable knobs live in Store via app.self_params — not frozen magic in modules.
SELF_TUNABLE = 'self_params_v1'
IMMUTABLE = (
    'capital_no_volume_dom',
    'feature_layout_base_dynamic_external',
    'actions_buy_sell_hold',
    'broker_tick_min_stop_margin',
)
EXECUTION_GATES = (
    'manual_first_close',
    'news_calendar',
    'tradeable_market',
    'one_position',
    'order_reconciliation',
    'fusion_ready_after_v4',
)
