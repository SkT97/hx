"""Test package for the public ``hx`` export.

The bootstrap below makes the suite importable straight from a checkout, with
no install step: ``python -m unittest discover`` run in ``public-export/hx``
works, as does ``python -m unittest tests.test_accept`` and ``pytest``.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
