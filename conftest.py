"""Pytest bootstrap: put the repo root on sys.path so `from src import ...` works
regardless of the directory pytest is invoked from."""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
