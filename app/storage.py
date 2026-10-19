"""Persistent writable application storage for both source and frozen Windows builds."""
import os
import sys
from pathlib import Path

PROJECT_ROOT=Path(__file__).resolve().parent.parent
if getattr(sys,'frozen',False):
    default=Path(os.getenv('LOCALAPPDATA') or Path.home())/'NeuralTradingAI'
else:
    default=PROJECT_ROOT/'data'
DATA=Path(os.getenv('NEURAL_AI_DATA_DIR') or default)
DATA.mkdir(parents=True,exist_ok=True)
