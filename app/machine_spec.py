"""Single coherent contract for the self-learning trading machine.

This module does not execute trades. It documents the LIVE decision path so
version history (v3 features / v4 fusion / v6 argmax) is not confused with
competing LIVE vetoes. Full Latvian explanation: PASMACISANAS_MASINA_LV.md
"""
from .core import (
    NeuralCore,
    OBJECTIVE_WEIGHTS,
    MINUTE_VS_TRADE_WEIGHT_RATIO,
    OBJECTIVE_MINUTE_DIRECTION,
    OBJECTIVE_TRADE_PNL,
)
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
PRIMARY_FEEDBACK = 'closed_trade_pnl_counterfactual_buy_sell_hold'
# Audit #17: two objectives, relative weights (trade_pnl is 5× minute_direction).
LEARN_OBJECTIVES = {
    OBJECTIVE_MINUTE_DIRECTION: OBJECTIVE_WEIGHTS[OBJECTIVE_MINUTE_DIRECTION],
    OBJECTIVE_TRADE_PNL: OBJECTIVE_WEIGHTS[OBJECTIVE_TRADE_PNL],
}
# MINUTE_VS_TRADE_WEIGHT_RATIO re-exported from core (0.2).
# Audit #18: snapshot / bound ΔW / restore on CE worsen; never proven on ordinary learn.
WEIGHT_UPDATE_POLICY = 'snapshot_bound_delta_restore_on_ce_worsen_never_proven'
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
# Public multi-source liquidity is resolved per instrument (FX futures, metals,
# energy, indices, crypto spot+L2) as EXTERNAL fusion only — never a Capital fill.
DISCOVERY_DATA = ('ohlc','spread','peers','scheduled_news')
EXTERNAL_LIQUIDITY = ('yahoo_futures_etf_volume','coinbase_spot_volume_l2',
                      'okx_spot_volume_backup','per_instrument_route_resolver')
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
    # Fusion preferred when ready; transient peer/liquidity gaps keep last neural vote.
)
